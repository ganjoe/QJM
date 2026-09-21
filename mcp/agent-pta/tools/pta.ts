import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, getActiveTradingMode, log } from "./shared.ts";

// ---------------------------------------------------------------------------
// Broker-Truth-Snapshot — Frische-Kontrakt
// ---------------------------------------------------------------------------
// pta_ibkr_positions / pta_ibkr_account_summary sind die EINZIGE Quelle der
// Depotwerte. Sie werden von ibkr-sync geschrieben (periodisch + auf Anfrage).
// Ein Wert ohne Zeitstempel ist wertlos: er sieht identisch aus, egal ob er
// 3 Sekunden oder 3 Tage alt ist. Genau daran ist die Portfolio-Anzeige
// zuvor gescheitert — sie lieferte wochenlang eingefrorene Kurse aus, ohne
// das kenntlich zu machen. Deshalb gilt hier:
//   * Jeder Refresh-Auftrag trägt einen EXPLIZITEN notes-Zustand.
//     (Die Spalte pta_execution_log.notes hat keinen DB-Default; ein Insert
//      ohne notes erzeugt NULL und fällt durch jeden notes-Filter im Daemon.)
//   * Ein nicht bestätigter Refresh wird nie verschwiegen.
//   * Jede Antwort nennt den Snapshot-Zeitpunkt und dessen Alter.
// Details: docs/architecture/broker-truth-snapshot.md
const REFRESH_STATE = {
  /** Vom Tool gesetzt: Auftrag liegt bereit. */
  PENDING: "PENDING",
  /** Vom Daemon gesetzt, waehrend er den Snapshot schreibt. */
  PROCESSING: "PROCESSING",
  /** Vom Daemon gesetzt bei Erfolg. */
  COMPLETED: "COMPLETED",
  /**
   * Vom Daemon gesetzt, wenn er geantwortet hat, aber nicht schreiben konnte
   * (z. B. "ERROR: NO_ACCOUNT_METRICS"). Muss getrennt behandelt werden: sonst
   * laeuft der Poll in den Timeout und meldet faelschlich "nicht abgeholt" —
   * obwohl der Daemon sehr wohl geantwortet hat.
   */
  ERROR_PREFIX: "ERROR:",
} as const;

/** Wartezeit auf die Bestätigung des Daemons (20 x 500 ms = 10 s). */
const REFRESH_POLL_ATTEMPTS = 20;
const REFRESH_POLL_INTERVAL_MS = 500;

/** Ab diesem Alter gilt der Snapshot als veraltet und wird als solcher ausgewiesen. */
const SNAPSHOT_STALE_AFTER_SEC = 90;

/** Neuester Zeitstempel aus einer Menge von updated_at-Werten. */
function latestTimestamp(candidates: Array<string | null | undefined>): string | null {
  let best: string | null = null;
  let bestMs = -Infinity;
  for (const c of candidates) {
    if (!c) continue;
    const ms = new Date(c).getTime();
    if (!Number.isFinite(ms)) continue;
    if (ms > bestMs) {
      bestMs = ms;
      best = c;
    }
  }
  return best;
}

/** Alter des aktuell gespeicherten Snapshots in Sekunden (null = kein Snapshot vorhanden). */
async function readSnapshotAgeSec(mode: "live" | "paper"): Promise<number | null> {
  try {
    const candidates: Array<string | null | undefined> = [];

    const { data: posRows } = await supabase
      .from("pta_ibkr_positions")
      .select("updated_at")
      .eq("mode", mode)
      .order("updated_at", { ascending: false })
      .limit(1);
    candidates.push(posRows?.[0]?.updated_at);

    const { data: accRow } = await supabase
      .from("pta_ibkr_account_summary")
      .select("updated_at")
      .eq("mode", mode)
      .order("updated_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    candidates.push(accRow?.updated_at);

    const newest = latestTimestamp(candidates);
    if (!newest) return null;
    const ms = new Date(newest).getTime();
    return Number.isFinite(ms) ? (Date.now() - ms) / 1000 : null;
  } catch (e) {
    // Die Vorprüfung darf den Abruf nicht verhindern. Im Zweifel wird ein
    // Refresh angefordert (Rückgabe null) — das entspricht dem Verhalten vor
    // dieser Änderung und ist immer noch besser als ein abgebrochener Abruf.
    log.warn(`[Snapshot] Altersprüfung fehlgeschlagen, fordere Refresh an: ${e}`);
    return null;
  }
}

/**
 * Kopfzeile jeder Portfolio-Antwort: Zeitpunkt, Alter, Bewertung.
 * Bewusst als erste Zeile — vor jeder Zahl, damit kein Wert ohne
 * Haltbarkeitsdatum gelesen werden kann.
 */
function refreshFailureNote(refreshConfirmed: boolean, refreshError: string | null): string {
  if (refreshConfirmed) return "";
  if (refreshError) {
    return `Der Daemon hat den Refresh beantwortet, konnte den Snapshot aber nicht schreiben (${refreshError}). ` +
      `Die Ursache liegt beim Kontozustand, nicht bei der Zustellung.\n`;
  }
  return "Der angeforderte Refresh wurde nicht bestätigt — ibkr-sync hat den Auftrag nicht abgeholt.\n";
}

function formatSnapshotHeader(
  snapshotAt: string | null,
  ageSec: number | null,
  isStale: boolean,
  refreshConfirmed: boolean,
  refreshError: string | null = null,
): string {
  if (!snapshotAt) {
    return "=== SNAPSHOT: KEINER VORHANDEN ===\n" +
      "Es liegt noch kein Broker-Snapshot vor. Die folgenden Bereiche sind leer oder unvollständig.\n" +
      refreshFailureNote(refreshConfirmed, refreshError);
  }

  const age = ageSec === null ? "?" : `${ageSec.toFixed(1)}s`;
  const state = isStale
    ? "⚠️ VERALTET"
    : (refreshConfirmed ? "✅ frisch" : "⚠️ nicht bestätigt");
  const legend = isStale
    ? `\n⚠️ Der Snapshot ist älter als ${SNAPSHOT_STALE_AFTER_SEC}s. Die folgenden Depotwerte weichen ` +
      `mit hoher Wahrscheinlichkeit von dem ab, was der Broker beim Login zeigt.` +
      (refreshConfirmed ? "" : `\n${refreshFailureNote(refreshConfirmed, refreshError)}`)
    : "";

  return `=== SNAPSHOT: ${snapshotAt} (Alter: ${age}) — ${state} ===${legend}`;
}

/**
 * Leitet aus Ledger-Zeilen (pta_active_positions) die Exit-Richtung ab.
 * BUY = Short covern, SELL = Long schließen. Gemischte Richtungen => null (nicht raten).
 */
function exitSideFromLedgerRows(rows: any[] | null): "BUY" | "SELL" | null {
  if (!rows || rows.length === 0) return null;
  let longQty = 0;
  let shortQty = 0;
  for (const r of rows) {
    const raw = Number(r?.net_quantity) || 0;
    const q = Math.abs(raw);
    const isShort = String(r?.position_type || "").toUpperCase() === "SHORT" || raw < 0;
    if (isShort) shortQty += q; else longQty += q;
  }
  if (longQty > 0 && shortQty > 0) return null;
  if (shortQty > 0) return "BUY";
  if (longQty > 0) return "SELL";
  return null;
}

/**
 * Ermittelt die EXIT-Richtung ausschließlich innerhalb des aktiven Modus.
 * Liefert null, wenn keine eindeutige offene Position existiert — dann darf KEIN
 * blinder SELL-Fallback gesendet werden, sonst eröffnet ein EXIT auf ein flaches
 * Konto einen Short.
 *
 * Reihenfolge:
 *  - STK: Broker-Snapshot (mode+ticker, über Konten aggregiert) ist maßgeblich,
 *         danach Ledger (mode+trade_id), danach Ledger (mode+ticker).
 *  - OPT/COMBO: Ledger (mode+trade_id) zuerst, da der Broker-Snapshot keine
 *         Kontrakt-Identität trägt; danach Ledger (mode+ticker).
 */
async function resolveExitSide(
  assetType: string,
  ticker: string,
  tradeId: string | undefined,
  activeMode: "live" | "paper",
): Promise<"BUY" | "SELL" | null> {
  const sym = String(ticker || "").trim().toUpperCase();

  if (assetType === "STK") {
    const { data: posRows, error: posErr } = await supabase
      .from("pta_ibkr_positions")
      .select("quantity")
      .eq("mode", activeMode)
      .eq("ticker", sym);
    if (posErr) throw new Error(`Positionsabfrage fehlgeschlagen: ${posErr.message}`);

    const netQty = (posRows || []).reduce((sum: number, r: any) => sum + (Number(r?.quantity) || 0), 0);
    if (netQty !== 0) return netQty < 0 ? "BUY" : "SELL";
  }

  if (tradeId) {
    const { data: byTrade, error: tradeErr } = await supabase
      .from("pta_active_positions")
      .select("net_quantity, position_type")
      .eq("mode", activeMode)
      .eq("trade_id", tradeId);
    if (tradeErr) throw new Error(`Ledger-Abfrage fehlgeschlagen: ${tradeErr.message}`);
    const side = exitSideFromLedgerRows(byTrade);
    if (side) return side;
  }

  const { data: byTicker, error: tickerErr } = await supabase
    .from("pta_active_positions")
    .select("net_quantity, position_type")
    .eq("mode", activeMode)
    .eq("ticker", sym);
  if (tickerErr) throw new Error(`Ledger-Abfrage fehlgeschlagen: ${tickerErr.message}`);
  return exitSideFromLedgerRows(byTicker);
}

export function registerPtaTools(server: McpServer) {
  // Tool: Place Trade (Execution Logger)
  server.registerTool(
    "place_trade",
    {
      title: "Place Trade",
      description: "Submits a trade to the broker by logging an execution event into the database, which is then asynchronously picked up and executed by the gateway. Supports STK, OPT, or COMBO. Valid actions are ENTER, UPDATE, EXIT, CANCEL, CASH, and REFRESH.",
      inputSchema: {
        action: z.enum(["ENTER", "UPDATE", "EXIT", "CANCEL", "CASH", "REFRESH"]).describe("The execution action"),
        asset_type: z.enum(["STK", "OPT", "COMBO"]).default("STK").describe("The asset type"),
        trade_id: z.string().describe("The unique Trade-ID (generated by STM)"),
        ticker: z.string().describe("The stock symbol"),
        quantity: z.number().optional().describe("Amount of shares/contracts"),
        limit_price: z.number().optional().describe("Limit price for orders (net premium for combos)"),
        stop_loss: z.number().optional().describe("Stop loss price (mainly STK)"),
        take_profit: z.number().optional().describe("Take profit limit price (mainly STK)"),
        broker_order_id: z.string().optional().describe("Optional broker-provided order ID"),
        commission: z.number().optional().default(0),
        currency: z.string().optional().default("USD"),
        exchange: z.string().optional().describe("Target exchange (e.g., SMART)"),
        notes: z.string().optional().describe("Technical notes (For OPT/COMBO, this is auto-generated)"),
        // Option specific
        expiry: z.string().optional().describe("Expiration date in YYYYMMDD format (OPT only)"),
        strike: z.number().optional().describe("Strike price (OPT only)"),
        right: z.enum(["C", "P"]).optional().describe("Call (C) or Put (P) (OPT only)"),
        multiplier: z.number().optional().default(100).describe("Option multiplier (OPT only)"),
        // Combo specific
        comboLegs: z.array(z.object({
            expiry: z.string().describe("Expiration date in YYYYMMDD format"),
            strike: z.number().describe("Strike price"),
            right: z.enum(["C", "P"]).describe("Call (C) or Put (P)"),
            action: z.enum(["BUY", "SELL"]).describe("Leg action (BUY or SELL)"),
            ratio: z.number().default(1).describe("Ratio of this leg")
        })).optional().describe("Array of legs for the combination (COMBO only)"),
      },
    },
    async (params: any) => {
      try {
        // Resolve the active mode once, up front: the EXIT branch below creates its
        // auto-cancel row and must tag it with the same mode the order is written in.
        // Without this the row fell back to the 'live' column default, so a paper EXIT
        // produced a LIVE cancel request that the mode-scoped daemon then ignored.
        const activeMode = await getActiveTradingMode();

        // Guard: REFRESH has no branch in this handler. Without this guard it fell
        // through, wrote an ORDER_SUBMITTED row with action "REFRESH", and the sync
        // daemon resolved that to a BUY market order. Refresh belongs to
        // list_active_positions(), which triggers the snapshot and waits for it.
        if (params.action === "REFRESH") {
          return {
            content: [{ type: "text", text: "REFRESH is not a valid place_trade action (it would have been sent to the broker as a BUY order). Use `list_active_positions()` instead — it triggers the broker snapshot refresh and waits for it." }],
            isError: true
          };
        }

        let eventType = "ORDER_SUBMITTED";
        let action = params.action;

        if (params.action === "CANCEL") {
            // First check if there is an unconfirmed order with this trade_id
            const { data: unconfirmed, error: unconfirmedErr } = await supabase
                .from("pta_execution_log")
                .select("id")
                .eq("trade_id", params.trade_id)
                .is("broker_order_id", null)
                .eq("event_type", "ORDER_SUBMITTED");
                
            if (unconfirmedErr) throw unconfirmedErr;
            if (unconfirmed && unconfirmed.length > 0) {
                // Locally cancel them by marking broker_order_id as CANCELLED
                const { error: updateErr } = await supabase
                    .from("pta_execution_log")
                    .update({ broker_order_id: "CANCELLED", notes: "Cancelled locally by agent before submission" })
                    .in("id", unconfirmed.map(u => u.id));
                if (updateErr) throw updateErr;
                
                return { 
                  content: [{ 
                    type: "text", 
                    text: `Unconfirmed order(s) for Trade-ID ${params.trade_id} successfully cancelled locally. They will not be sent to the broker.` 
                  }] 
                };
            }

            eventType = "CANCEL_REQUESTED";
            action = "CANCEL";
        } else if (params.action === "CASH") {
            eventType = "CASH_TRANSFER";
            action = params.quantity && params.quantity > 0 ? "DEPOSIT" : "WITHDRAW";
        } else if (params.action === "EXIT") {
            // Richtung ausschließlich im aktiven Modus bestimmen; kein blinder SELL-Fallback.
            const exitSide = await resolveExitSide(params.asset_type, params.ticker, params.trade_id, activeMode);
            if (!exitSide) {
                return {
                    content: [{
                        type: "text",
                        text: `EXIT abgebrochen: Für ${params.ticker} wurde im Modus ${activeMode.toUpperCase()} keine eindeutige offene Position gefunden (weder aktiver Broker-Snapshot noch Ledger). Es wurde KEINE Order erzeugt — EXIT darf niemals versehentlich eine neue Position (Short) eröffnen.`
                    }],
                    isError: true
                };
            }
            action = exitSide;

            if (params.asset_type === "STK") {
                const { error: cancelLogErr } = await supabase.from("pta_execution_log").insert({
                    trade_id: params.trade_id || "SYSTEM",
                    ticker: params.ticker,
                    event_type: "CANCEL_REQUESTED",
                    action: "CANCEL",
                    notes: "Auto-cancelling open orders due to EXIT action",
                    mode: activeMode
                });
                if (cancelLogErr) console.error("Failed to auto-cancel orders on EXIT", cancelLogErr);
            }
        } else if (params.action === "ENTER") {
            action = "BUY";
        }

        let p_notes = params.notes || null;
        if (params.asset_type === "OPT") {
            const optionParams = {
                expiry: params.expiry,
                strike: params.strike,
                right: params.right,
                multiplier: params.multiplier,
                isOption: true
            };
            p_notes = JSON.stringify(optionParams);
        } else if (params.asset_type === "COMBO") {
            const comboParams = {
                legs: params.comboLegs,
                isCombo: true
            };
            p_notes = JSON.stringify(comboParams);
        }

        const { data, error } = await supabase.rpc("pta_log_event", {
          p_trade_id: params.trade_id,
          p_ticker: params.ticker,
          p_event_type: eventType,
          p_action: action,
          p_quantity: Math.abs(params.quantity || 0),
          p_price: params.limit_price || null,
          p_stop_price: params.stop_loss || null,
          p_broker_order_id: params.broker_order_id || null,
          p_commission: params.commission || 0,
          p_currency: params.currency || "USD",
          p_exchange: params.exchange || (params.asset_type !== "STK" ? "SMART" : null),
          p_notes: p_notes,
          p_take_profit: params.take_profit || null,
          p_mode: activeMode
        });

        if (error) throw error;

        let gatewayWarning = "";
        if (eventType !== "REFRESH_REQUESTED" && eventType !== "CASH_TRANSFER") {
            const { data: sysData } = await supabase
                .from("system_settings")
                .select("value")
                .eq("key", "ib_gateway_status")
                .single();
            if (sysData && sysData.value && sysData.value.connected === false) {
                gatewayWarning = "\nHINWEIS: Order wurde lokal gespeichert, aber das IB-Gateway ist offline. Die Order wird an den Broker übermittelt, sobald es wieder online ist.";
            }
        }

        let unconfirmedWarning = "";
        if (eventType === "ORDER_SUBMITTED") {
            const { data: unconfirmedList } = await supabase
                .from("pta_execution_log")
                .select("trade_id")
                .eq("ticker", params.ticker)
                .is("broker_order_id", null)
                .eq("event_type", "ORDER_SUBMITTED")
                .neq("id", data);
                
            if (unconfirmedList && unconfirmedList.length > 0) {
                const ids = unconfirmedList.map(u => u.trade_id).join(", ");
                unconfirmedWarning = `\nWARNUNG: Es gibt noch ${unconfirmedList.length} weitere unbestätigte Order(s) für diesen Ticker in der lokalen Warteschlange. (Trade-IDs: ${ids})`;
            }
        }

        // Poll for up to 5 seconds to wait for broker confirmation
        if (eventType === "ORDER_SUBMITTED" || eventType === "CANCEL_REQUESTED") {
            let attempts = 0;
            while (attempts < 10) {
                await new Promise(resolve => setTimeout(resolve, 500));
                const { data: updatedEvent } = await supabase
                    .from("pta_execution_log")
                    .select("broker_order_id, notes")
                    .eq("id", data)
                    .single();
                
                if (updatedEvent && updatedEvent.broker_order_id) {
                    if (updatedEvent.broker_order_id === "CANCELLED") {
                         return { content: [{ type: "text", text: `Event logged successfully (ID: ${data}). ${updatedEvent.notes || 'Order cancelled successfully.'}${gatewayWarning}${unconfirmedWarning}` }] };
                    } else if (updatedEvent.broker_order_id === "NONE_FOUND") {
                         return { content: [{ type: "text", text: `Event logged (ID: ${data}), but no matching order was found to cancel at the broker.${gatewayWarning}${unconfirmedWarning}` }] };
                    }
                    return { content: [{ type: "text", text: `Event logged successfully (ID: ${data}). Broker Order ID: ${updatedEvent.broker_order_id}. Action: ${params.action} for ${params.ticker} (Trade: ${params.trade_id})${gatewayWarning}${unconfirmedWarning}` }] };
                }
                attempts++;
            }
            return { content: [{ type: "text", text: `Event logged in database (ID: ${data}), but no immediate confirmation received from IB Gateway. The order is queued and will be processed soon.${gatewayWarning}${unconfirmedWarning}` }] };
        }

        return { 
          content: [{ 
            type: "text", 
            text: `Event logged successfully (ID: ${data}). Action: ${params.action} for ${params.ticker} (Trade: ${params.trade_id})${gatewayWarning}${unconfirmedWarning}` 
          }] 
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error logging trade: ${err.message}` }], isError: true };
      }
    }
  );

  // Tool: List Active Positions
  server.registerTool(
    "list_active_positions",
    {
      title: "List Active Positions",
      description: "Shows all currently open trades and their net positions from the broker snapshot. This tool requests a broker snapshot refresh, provides the current account summary (such as Cash Balance and Net Liquidation Value), lists all unconfirmed or queued orders, and displays locally tracked trades. Use this tool if you need to know the account's cash balance, check for hanging orders, or see the current status of the portfolio.\n\n" +
        "IMPORTANT — data freshness: the answer ALWAYS starts with a '=== SNAPSHOT: ... ===' header giving the timestamp and age of the underlying broker snapshot. The figures below that header are only as current as that snapshot. If the header says '⚠️ VERALTET' (older than 90s) or 'nicht bestätigt', the numbers MUST NOT be presented to the user as the current portfolio — say explicitly that the snapshot is stale and that the broker app is the authoritative source.",
      inputSchema: {
        ticker: z.string().optional().describe("Optional filter by ticker"),
      },
    },
    async ({ ticker }: any) => {
      try {
        const activeMode = await getActiveTradingMode();

        // ── Snapshot-Refresh ────────────────────────────────────────────────
        // Im Regelbetrieb schreibt ibkr-sync den Snapshot selbst periodisch
        // (alle 30 s). Dann ist hier NICHTS zu tun: dieser Handler bleibt ein
        // reiner Lesezugriff und berührt den Order-Kanal nicht.
        // Nur wenn der vorliegende Stand veraltet ist, wird ein Refresh
        // angefordert. Das deckt zwei Fälle ab: einen Daemon, der noch nicht
        // periodisch schreibt, und einen, der gerade nicht verbunden ist.
        const preAgeSec = await readSnapshotAgeSec(activeMode);
        const needsRefresh = preAgeSec === null || preAgeSec > SNAPSHOT_STALE_AFTER_SEC;
        let refreshConfirmed = !needsRefresh;
        let refreshError: string | null = null;

        if (needsRefresh) {
          // R1. Refresh anfordern.
          //    `notes` MUSS gesetzt werden (die Spalte hat keinen DB-Default) und
          //    `mode` ebenfalls — sonst übernimmt die Spalte ihren Default 'live'
          //    und ein Refresh aus PAPER wäre als LIVE-Auftrag markiert.
          const { data: refreshData, error: insertErr } = await supabase.from("pta_execution_log").insert({
            trade_id: "SYSTEM",
            ticker: "SYSTEM",
            event_type: "REFRESH_REQUESTED",
            action: "REFRESH",
            quantity: 0,
            price: 0,
            notes: REFRESH_STATE.PENDING,
            mode: activeMode
          }).select("id").single();

          if (insertErr || !refreshData) throw new Error("Failed to create REFRESH_REQUESTED event");
          const refreshId = refreshData.id;

          // R2. Auf die Antwort des Daemons warten (COMPLETED oder ERROR:...).
          //     Ein Fehlerzustand bricht sofort ab — sonst liefe der Poll in den
          //     Timeout und meldete "nicht abgeholt", obwohl geantwortet wurde.
          for (let attempt = 0; attempt < REFRESH_POLL_ATTEMPTS; attempt++) {
              await new Promise(resolve => setTimeout(resolve, REFRESH_POLL_INTERVAL_MS));
              const { data: checkData } = await supabase
                  .from("pta_execution_log")
                  .select("notes")
                  .eq("id", refreshId)
                  .single();
              const state = String(checkData?.notes ?? "");
              if (state === REFRESH_STATE.COMPLETED) {
                  refreshConfirmed = true;
                  break;
              }
              if (state.startsWith(REFRESH_STATE.ERROR_PREFIX)) {
                  refreshError = state;
                  break;
              }
          }

          // R3. Auftragszeile aufräumen. Bei Erfolg wird sie entfernt, im
          //    Fehlerfall bleibt sie als Nachweis stehen — sie wird nicht erneut
          //    abgeholt, weil der Daemon nur auf PENDING selektiert.
          //    Eine fehlende Bestätigung wird NICHT verschluckt: sie erscheint
          //    in der Kopfzeile der Antwort.
          if (refreshConfirmed) {
            await supabase.from("pta_execution_log").delete().eq("id", refreshId);
          } else {
            log.warn(
              `[Snapshot] REFRESH_REQUESTED (id ${refreshId}, mode ${activeMode}) blieb unbeantwortet ` +
              `(${(REFRESH_POLL_ATTEMPTS * REFRESH_POLL_INTERVAL_MS) / 1000}s). ` +
              (refreshError
                ? `Der Daemon hat mit "${refreshError}" geantwortet, konnte den Snapshot also nicht schreiben.`
                : `ibkr-sync hat den Auftrag nicht abgeholt (nicht verbunden, oder notes-Filter greift nicht).`) +
              ` Ausgeliefert wird der letzte erfolgreiche Snapshot — Alter siehe Kopfzeile.`,
            );
          }
        } else {
          log.debug(`[Snapshot] Stand ist ${preAgeSec?.toFixed(1)}s alt — kein Refresh nötig, reiner Lesezugriff.`);
        }

        // 3. Fetch local active positions (filtered by active mode — critical for paper/live isolation)
        let activeQuery = supabase.from("pta_active_positions").select("*").eq("mode", activeMode);
        if (ticker) activeQuery = activeQuery.eq("ticker", ticker.toUpperCase());
        const { data: activeData, error: activeErr } = await activeQuery;
        if (activeErr) throw activeErr;

        // 4. Fetch live broker positions (filtered by active mode)
        let liveQuery = supabase.from("pta_ibkr_positions").select("*").eq("mode", activeMode);
        if (ticker) liveQuery = liveQuery.eq("ticker", ticker.toUpperCase());
        const { data: liveData, error: liveErr } = await liveQuery;
        if (liveErr) throw liveErr;

        // 5. Fetch account summary (filtered by active mode).
        //    Sortierung nach updated_at: es kann mehrere Konten je Modus geben,
        //    und die Frischeanzeige muss den jüngsten Stand bewerten — nicht
        //    eine beliebige Zeile.
        const { data: accData, error: accErr } = await supabase
          .from("pta_ibkr_account_summary").select("*")
          .eq("mode", activeMode)
          .order("updated_at", { ascending: false })
          .limit(1).maybeSingle();
        if (accErr) throw accErr;

        // 6. Fetch live open orders (filtered by active mode)
        let ordersQuery = supabase.from("pta_ibkr_open_orders").select("*").eq("mode", activeMode);
        if (ticker) ordersQuery = ordersQuery.eq("ticker", ticker.toUpperCase());
        const { data: ordersData, error: ordersErr } = await ordersQuery;
        if (ordersErr) throw ordersErr;

        // 6.5 Fetch unconfirmed local orders (filtered by active mode)
        let unconfirmedQuery = supabase.from("pta_execution_log")
          .select("*")
          .is("broker_order_id", null)
          .eq("mode", activeMode)
          .in("event_type", ["ORDER_SUBMITTED", "CANCEL_REQUESTED"]);
        if (ticker) unconfirmedQuery = unconfirmedQuery.eq("ticker", ticker.toUpperCase());
        const { data: unconfirmedData, error: unconfirmedErr } = await unconfirmedQuery;
        if (unconfirmedErr) throw unconfirmedErr;

        // 7. Fetch exchange rates from DB
        let rates: Record<string, number> = { "EUR": 1.0 };
        try {
          const currencies = new Set((liveData || []).map((p: any) => p.currency));
          const nonEur = Array.from(currencies).filter(c => c && c !== "EUR");
          
          if (nonEur.length > 0) {
            const { data: fxData, error: fxErr } = await supabase
              .from('exchange_rates')
              .select('*')
              .in('target_currency', nonEur)
              .eq('base_currency', 'EUR')
              .order('date', { ascending: false });

            if (!fxErr && fxData) {
              const seen = new Set();
              for (const row of fxData) {
                if (!seen.has(row.target_currency)) {
                  rates[row.target_currency] = 1.0 / row.rate;
                  seen.add(row.target_currency);
                }
              }
            }
          }
        } catch (e) {
          console.error("Failed to fetch exchange rates from DB", e);
        }

        // Map open times from local tracked trades
        const openTimesByTicker: Record<string, string> = {};
        for (const p of activeData || []) {
            if (p.open_time) {
                openTimesByTicker[p.ticker] = p.open_time;
            }
        }
        
        // Helper to calculate days out
        const calculateDaysOut = (openTimeIso: string) => {
            if (!openTimeIso) return null;
            const openTime = new Date(openTimeIso).getTime();
            const now = new Date().getTime();
            return ((now - openTime) / (1000 * 3600 * 24)).toFixed(1);
        };

        // Snapshot-Frische bestimmen: maßgeblich ist der jüngste updated_at-Wert
        // der Positionszeilen, ersatzweise der des Kontostands (ein flaches Konto
        // hat keine Positionszeilen, ist aber trotzdem ein gültiger Snapshot).
        const snapshotAt = latestTimestamp([
          ...(liveData || []).map((p: any) => p.updated_at),
          accData?.updated_at,
        ]);
        const snapshotAgeSec = snapshotAt ? (Date.now() - new Date(snapshotAt).getTime()) / 1000 : null;
        const snapshotStale = snapshotAgeSec === null || snapshotAgeSec > SNAPSHOT_STALE_AFTER_SEC;

        let responseText = `=== TRADING MODE: ${activeMode.toUpperCase()} ===\n`;
        responseText += formatSnapshotHeader(snapshotAt, snapshotAgeSec, snapshotStale, refreshConfirmed, refreshError) + "\n\n";

        // Format account summary
        if (accData && !ticker) {
          // Number(... ?? 0): eine einzelne NULL-Spalte darf nicht die gesamte
          // Portfolio-Antwort mit einem TypeError zerstören.
          const eur = (v: any) => Number(v ?? 0).toFixed(2);
          responseText += `=== ${activeMode.toUpperCase()} IBKR ACCOUNT SUMMARY (Base: EUR) ===\n`;
          responseText += `- Total Cash Balance: ${eur(accData.total_cash_balance)} EUR\n`;
          responseText += `- Net Liquidation Value: ${eur(accData.net_liquidation)} EUR\n`;
          responseText += `- Available Funds: ${eur(accData.available_funds)} EUR\n`;
          responseText += `- Cash Quote: ${eur(accData.cash_quote)}%\n\n`;
        } else if (!ticker) {
          responseText += `No account summary available for ${activeMode} mode yet.\n\n`;
        }

        // Group open orders by ticker
        const ordersByTicker: Record<string, any[]> = {};
        for (const o of ordersData || []) {
          if (!ordersByTicker[o.ticker]) ordersByTicker[o.ticker] = [];
          ordersByTicker[o.ticker].push(o);
        }

        // Format live broker positions
        if (liveData && liveData.length > 0) {
          responseText += "=== LIVE IBKR PORTFOLIO STATUS (from Broker) ===\n";
          const lines = liveData.map((p: any) => {
            const curr = p.currency || "USD";
            const rate = rates[curr] || 1.0;
            const mktValueEur = (p.market_value || 0) * rate;
            const daysOutStr = openTimesByTicker[p.ticker] ? ` | Days Out: ${calculateDaysOut(openTimesByTicker[p.ticker])}` : "";

            let line = `- Ticker: ${p.ticker} | Qty: ${p.quantity} | Avg Cost: ${p.avg_cost?.toFixed(2) || '0.00'} ${curr} | Mkt Price: ${p.market_price?.toFixed(2) || '0.00'} ${curr} | Mkt Value: ${p.market_value?.toFixed(2) || '0.00'} ${curr} (~${mktValueEur.toFixed(2)} EUR) | Unrlz PnL: ${p.unrealized_pnl?.toFixed(2) || '0.00'} ${curr} | Rlz PnL: ${p.realized_pnl?.toFixed(2) || '0.00'} ${curr} | Weight: ${p.position_pct?.toFixed(2) || '0.00'}% | Account: ${p.account}${daysOutStr}`;
            
            const tickerOrders = ordersByTicker[p.ticker] || [];
            if (tickerOrders.length > 0) {
              for (const o of tickerOrders) {
                const priceStr = o.limit_price ? ` @ LMT ${o.limit_price.toFixed(2)}` : (o.stop_price ? ` @ STP ${o.stop_price.toFixed(2)}` : "");
                line += `\n    └─ [Active Order: ${o.action} ${o.quantity} ${o.order_type}${priceStr} (Status: ${o.status})]`;
              }
            }
            return line;
          });
          responseText += lines.join("\n") + "\n\n";
        } else {
          responseText += "=== LIVE IBKR PORTFOLIO STATUS (from Broker) ===\nNo active positions in IBKR account.\n\n";
        }

        // Format other open orders (which have no active position)
        const activeTickers = new Set((liveData || []).map((p: any) => p.ticker));
        const otherOrders = (ordersData || []).filter((o: any) => !activeTickers.has(o.ticker));
        if (otherOrders.length > 0 && !ticker) {
          responseText += "=== OTHER OPEN ORDERS (no active positions) ===\n";
          responseText += otherOrders.map((o: any) => {
            const priceStr = o.limit_price 
              ? ` | Lmt Price: ${o.limit_price.toFixed(2)}` 
              : (o.stop_price ? ` | Stop Price: ${o.stop_price.toFixed(2)}` : "");
            return `- Ticker: ${o.ticker} | Action: ${o.action} | Qty: ${o.quantity} | Type: ${o.order_type}${priceStr} | Status: ${o.status} | Account: ${o.account}`;
          }).join("\n") + "\n\n";
        }

        // Format unconfirmed orders
        if (unconfirmedData && unconfirmedData.length > 0) {
          responseText += "=== UNCONFIRMED / QUEUED ORDERS (LOCAL DB) ===\n";
          responseText += "WARNING: These orders are saved locally but not yet confirmed by the broker. You can cancel them using their Trade-ID.\n";
          responseText += unconfirmedData.map((o: any) => 
            `- [Trade-ID: ${o.trade_id}] ${o.ticker} | Action: ${o.action} | Qty: ${o.quantity} | Event: ${o.event_type} | Limit: ${o.price || 'N/A'} | Stop: ${o.stop_price || 'N/A'}`
          ).join("\n") + "\n\n";
        } else {
          responseText += "=== UNCONFIRMED / QUEUED ORDERS (LOCAL DB) ===\nNo unconfirmed orders pending.\n\n";
        }

        // Format active tracked trades
        if (activeData && activeData.length > 0) {
          responseText += "=== TRACKED TRADES (LOCAL DB) ===\n";
          responseText += activeData.map((p: any) => {
            const daysOutStr = p.open_time ? ` | Days Out: ${calculateDaysOut(p.open_time)}` : "";
            return `- [${p.trade_id}] ${p.ticker}: ${p.net_quantity} @ ${p.position_type} (SL: ${p.current_stop_loss || 'NONE'})${daysOutStr}`;
          }).join("\n");
        } else {
          responseText += "=== TRACKED TRADES (LOCAL DB) ===\nNo active tracked trades in local database.";
        }

        return { content: [{ type: "text", text: responseText }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error fetching positions: ${err.message}` }], isError: true };
      }
    }
  );

  // Tool: Get Trade History
  server.registerTool(
    "get_trade_history",
    {
      title: "Get Trade History",
      description: "Retrieve all execution events for a specific trade ID. Or retrieve a list of all historical closed trades if no trade ID is provided.",
      inputSchema: {
        trade_id: z.string().optional().describe("Optional. The Trade-ID to look up. If omitted, returns a list of recent closed trades."),
        limit: z.number().optional().describe("Optional. Number of recent trades to return when trade_id is omitted. Default 50."),
        start_date: z.string().optional().describe("Optional. Start date (YYYY-MM-DD) for historical trades list."),
        end_date: z.string().optional().describe("Optional. End date (YYYY-MM-DD) for historical trades list."),
        min_trade_index: z.number().optional().describe("Optional. Minimum sequential trade number (e.g. 150)"),
        max_trade_index: z.number().optional().describe("Optional. Maximum sequential trade number (e.g. 159)"),
      },
    },
    async ({ trade_id, limit, start_date, end_date, min_trade_index, max_trade_index }: any) => {
      try {
        const activeMode = await getActiveTradingMode();

        if (trade_id) {
          // Specific trade: fetch all events for this trade_id, filtered by mode
          const { data, error } = await supabase
            .from("pta_execution_log")
            .select("*")
            .eq("trade_id", trade_id)
            .eq("mode", activeMode)
            .order("created_at", { ascending: true });

          if (error) throw error;
          if (!data || data.length === 0) {
            return { content: [{ type: "text", text: `No history found for Trade-ID: ${trade_id} in ${activeMode} mode.` }] };
          }

          const history = data.map((e: any) => 
            `${new Date(e.created_at).toLocaleString()} | ${e.event_type} | ${e.action} | Qty: ${e.quantity} | Price: ${e.price || '-'} | OID: ${e.broker_order_id || '-'}`
          ).join("\n");

          return { content: [{ type: "text", text: `[${activeMode.toUpperCase()} MODE] History for ${trade_id}:\n\n${history}` }] };
        } else {
          // Closed trades list, filtered by mode
          let query = supabase.from("pta_trade_history").select("*").eq("mode", activeMode);

          if (start_date) query = query.gte("close_time", start_date);
          if (end_date) query = query.lte("close_time", end_date + "T23:59:59Z");
          if (min_trade_index) query = query.gte("trade_index", min_trade_index);
          if (max_trade_index) query = query.lte("trade_index", max_trade_index);

          const { data, error } = await query
            .order("close_time", { ascending: false })
            .limit(limit || 50);

          if (error) throw error;
          if (!data || data.length === 0) {
            return { content: [{ type: "text", text: `No closed trades found in ${activeMode} mode history.` }] };
          }

          const history = data.map((t: any) => {
            const daysOutStr = t.days_out !== null && t.days_out !== undefined ? ` | Days Out: ${Number(t.days_out).toFixed(1)}` : "";
            return `#${t.trade_index} | [${t.trade_id}] ${new Date(t.close_time).toLocaleDateString()} | ${t.ticker} | ${t.is_winner ? 'WIN' : 'LOSS'} | PnL: ${t.net_pnl} | Winrate: ${parseFloat(t.running_winrate).toFixed(1)}%${daysOutStr}`;
          }).join("\n");

          return { content: [{ type: "text", text: `[${activeMode.toUpperCase()} MODE] Recent ${data.length} Closed Trades:\n\n${history}` }] };
        }
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error fetching history: ${err.message}` }], isError: true };
      }
    }
  );


  // Tool: Portfolio Analytics
  server.registerTool(
    "portfolio_analytics",
    {
      title: "Portfolio Analytics",
      description: "Calculates historical performance statistics like Winrate, Total PnL, Profit Factor, and Average Win/Loss based on all closed trades. You can optionally filter the results by a specific timeframe. IMPORTANT: If you call this tool WITHOUT a timeframe filter (no start_date, end_date, or days), it will additionally return crucial Live Risk & Exposure metrics (such as Portfolio Heat, Core Risk, and NAV) as well as Aggregate Account Metrics (like Max Drawdown and Total Cash Injected). Use this tool without timeframe filters whenever you need to check the current portfolio risk parameters.",
      inputSchema: {
        start_date: z.string().optional().describe("Start date (YYYY-MM-DD)"),
        end_date: z.string().optional().describe("End date (YYYY-MM-DD)"),
        days: z.number().optional().describe("Number of days looking back from today (e.g. 30 for last 30 days)"),
      },
    },
    async ({ start_date, end_date, days }: any) => {
      try {
        const activeMode = await getActiveTradingMode();

        let query = supabase
          .from("pta_trade_performance")
          .select("*")
          .eq("is_closed", true)
          .eq("mode", activeMode);   // mode filter

        if (days) {
          const pastDate = new Date();
          pastDate.setDate(pastDate.getDate() - days);
          query = query.gte("close_time", pastDate.toISOString());
        } else {
          if (start_date) query = query.gte("close_time", new Date(start_date).toISOString());
          if (end_date) query = query.lte("close_time", new Date(end_date).toISOString());
        }

        const { data: closedTrades, error: closedErr } = await query;
        if (closedErr) throw closedErr;

        let grossProfit = 0;
        let grossLoss = 0;
        let winningCount = 0;
        let losingCount = 0;
        let totalRealizedPnl = 0;
        let totalCommissions = 0;
        
        let daysOutAll: number[] = [];
        let daysOutWinners: number[] = [];
        let daysOutLosers: number[] = [];
        let rMultAll: number[] = [];
        let rMultWinners: number[] = [];
        let rMultLosers: number[] = [];

        for (const t of closedTrades || []) {
          totalRealizedPnl += t.net_pnl_eur;
          totalCommissions += t.total_commission;
          if (t.days_out !== null && t.days_out !== undefined) {
              const d = Number(t.days_out);
              daysOutAll.push(d);
              if (t.is_winner) daysOutWinners.push(d);
              else daysOutLosers.push(d);
          }
          if (t.r_multiple !== null && t.r_multiple !== undefined) {
              const r = Number(t.r_multiple);
              rMultAll.push(r);
              if (t.is_winner) rMultWinners.push(r);
              else rMultLosers.push(r);
          }
          if (t.is_winner) {
            grossProfit += t.net_pnl_eur;
            winningCount++;
          } else {
            grossLoss += Math.abs(t.net_pnl_eur);
            losingCount++;
          }
        }

        const totalClosed = closedTrades?.length || 0;
        const winrate = totalClosed > 0 ? (winningCount / totalClosed) * 100 : 0;
        const profitFactor = grossLoss > 0 ? (grossProfit / grossLoss) : (grossProfit > 0 ? 999 : 0);
        const avgWin = winningCount > 0 ? grossProfit / winningCount : 0;
        const avgLoss = losingCount > 0 ? grossLoss / losingCount : 0;
        const expectancy = (winrate / 100 * avgWin) - ((1 - winrate / 100) * avgLoss);
        
        const getMedian = (arr: number[]) => {
            if (arr.length === 0) return 0;
            const sorted = [...arr].sort((a, b) => a - b);
            const mid = Math.floor(sorted.length / 2);
            return sorted.length % 2 !== 0 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
        };
        const getAvg = (arr: number[]) => arr.length > 0 ? arr.reduce((a, b) => a + b, 0) / arr.length : 0;

        const avgDaysAll = getAvg(daysOutAll);
        const medianDaysAll = getMedian(daysOutAll);
        const avgDaysWin = getAvg(daysOutWinners);
        const medianDaysWin = getMedian(daysOutWinners);
        const avgDaysLoss = getAvg(daysOutLosers);
        const medianDaysLoss = getMedian(daysOutLosers);
        
        const avgRAll = getAvg(rMultAll);
        const avgRWin = getAvg(rMultWinners);
        const avgRLoss = getAvg(rMultLosers);

        let report = `=== PORTFOLIO ANALYTICS (mode: ${activeMode.toUpperCase()}) ===\n\n`;
        
        // Show overarching live risk and cash metrics if no specific timeframe is filtered
        if (!days && !start_date && !end_date) {
            // LIVE RISK bewusst DIREKT aus pta_ibkr_account_summary und modus-
            // gescoped gelesen:
            //  * Die View `pta_live_risk` hat WEDER eine mode-Spalte NOCH einen
            //    Filter. Gelesen mit `.limit(1).maybeSingle()` lieferte sie damit
            //    eine BELIEBIGE Zeile aus live ODER paper — im LIVE-Modus konnte
            //    so der PAPER-Kontostand als Portfolio-NAV erscheinen.
            //  * Der Snapshot-Zeitpunkt wird jetzt mit ausgewiesen (Frische-Kontrakt),
            //    sonst gilt hier dieselbe Blindheit wie früher in
            //    list_active_positions: ein eingefrorener Stand sieht aus wie ein
            //    frischer.
            const { data: riskData } = await supabase
                .from("pta_ibkr_account_summary")
                .select("account, net_liquidation, total_cash_balance, portfolio_heat_eur, core_risk_eur, updated_at")
                .eq("mode", activeMode)
                .order("updated_at", { ascending: false })
                .limit(1)
                .maybeSingle();

            report += `=== ${activeMode.toUpperCase()} LIVE RISK & EXPOSURE ===\n`;
            if (riskData) {
                const nav = Number(riskData.net_liquidation ?? 0);
                const heat = Number(riskData.portfolio_heat_eur ?? 0);
                const core = Number(riskData.core_risk_eur ?? 0);
                const pct = (v: number) => nav > 0 ? ((v / nav) * 100).toFixed(2) : "0.00";
                // Ein fehlender Zeitstempel gilt als veraltet — nie als frisch.
                const ageSec = riskData.updated_at
                    ? (Date.now() - new Date(riskData.updated_at).getTime()) / 1000
                    : null;
                const stale = ageSec === null || ageSec > SNAPSHOT_STALE_AFTER_SEC;

                report += formatSnapshotHeader(riskData.updated_at ?? null, ageSec, stale, true) + "\n";
                report += `Account: ${riskData.account}\n`;
                report += `Portfolio NAV: ${nav.toFixed(2)} EUR\n`;
                report += `Portfolio Heat: ${heat.toFixed(2)} EUR (${pct(heat)}%)\n`;
                report += `Core Risk: ${core.toFixed(2)} EUR (${pct(core)}%)\n\n`;
            } else {
                report += `Kein Kontostand-Snapshot fuer Modus '${activeMode}' vorhanden.\n\n`;
            }
            
            const { data: summaryData } = await supabase.from("pta_portfolio_summary").select("*").single();
            if (summaryData) {
                const eur = (v: any) => (v === null || v === undefined) ? "N/A" : Number(v).toFixed(2);
                report += `=== AGGREGATE ACCOUNT METRICS (Ledger: mode = 'live') ===\n`;
                if (activeMode === "paper") {
                    report += `⚠️ Die View pta_portfolio_summary ist fest auf mode = 'live' gesetzt. Im PAPER-Modus\n`;
                    report += `   sind die folgenden Kennzahlen LIVE-Werte, NICHT die des Paper-Kontos.\n`;
                }
                report += `Calculated Cash Balance (EUR): ${eur(summaryData.calculated_cash_balance_eur)}\n`;
                report += `Cash Injected (EUR): ${eur(summaryData.cash_injected_eur)}\n`;
                report += `Max Drawdown (EUR): ${eur(summaryData.max_drawdown_eur)}\n\n`;
            }
        }

        if (days) report += `Timeframe: Last ${days} days\n`;
        else if (start_date || end_date) report += `Timeframe: ${start_date || '...'} to ${end_date || '...'}\n`;
        else report += `Timeframe: All Time\n`;

        report += `\n== TRADES ==\n`;
        report += `- Closed Trades: ${totalClosed}\n`;
        report += `- Winning Trades: ${winningCount}\n`;
        report += `- Losing Trades: ${losingCount}\n\n`;
        
        report += `== PERFORMANCE ==\n`;
        report += `- Net Realized PnL: ${totalRealizedPnl.toFixed(2)}\n`;
        report += `- Total Commissions Paid: ${totalCommissions.toFixed(2)}\n`;
        report += `- Winrate: ${winrate.toFixed(2)}%\n`;
        report += `- Profit Factor: ${profitFactor.toFixed(2)}\n`;
        report += `- Expectancy (Net): ${expectancy.toFixed(2)} per trade\n\n`;

        report += `== AVERAGES (EUR) ==\n`;
        report += `- Average Win: ${avgWin.toFixed(2)}\n`;
        report += `- Average Loss: ${avgLoss.toFixed(2)}\n`;
        report += `- Gross Profit: ${grossProfit.toFixed(2)}\n`;
        report += `- Gross Loss: ${grossLoss.toFixed(2)}\n\n`;
        
        report += `== R-MULTIPLE ==\n`;
        report += `- Overall: Avg ${avgRAll.toFixed(2)}R\n`;
        report += `- Winners: Avg ${avgRWin.toFixed(2)}R\n`;
        report += `- Losers:  Avg ${avgRLoss.toFixed(2)}R\n\n`;
        
        report += `== HOLD TIME (Days Out) ==\n`;
        report += `- Overall: Avg ${avgDaysAll.toFixed(1)} | Median ${medianDaysAll.toFixed(1)}\n`;
        report += `- Winners: Avg ${avgDaysWin.toFixed(1)} | Median ${medianDaysWin.toFixed(1)}\n`;
        report += `- Losers:  Avg ${avgDaysLoss.toFixed(1)} | Median ${medianDaysLoss.toFixed(1)}\n`;

        return { content: [{ type: "text", text: report }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Error generating analytics: ${err.message}` }], isError: true };
      }
    }
  );
}
