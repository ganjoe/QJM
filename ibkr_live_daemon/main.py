import os
import json
import time
import asyncio
import logging
import threading
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiohttp import web
from ib_insync import IB, util, Contract, Order, Stock, Option, Bag, ComboLeg, LimitOrder, MarketOrder, StopOrder
from supabase import create_client, Client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("IBKR_SYNC")

IB_HOST_DEFAULT = os.getenv("IB_GATEWAY_HOST", "ib-gateway")
IB_PORT_DEFAULT = int(os.getenv("IB_GATEWAY_PORT", "4002"))

# Marktdatentyp fuer reqMarketDataType(): 1=Live, 2=Frozen, 3=Delayed, 4=Delayed-Frozen.
# ACHTUNG: betrifft ausschliesslich Marktdaten-Anfragen (reqTickers -> get_quote).
# Die Depotbewertung stammt aus den Account-Updates des Brokers und ist davon
# unabhaengig — der NAV ist also auch bei Typ 4 nicht verzoegert.
# Ueber IB_MARKET_DATA_TYPE umstellbar, ohne Code zu aendern.
IB_MARKET_DATA_TYPE = int(os.getenv("IB_MARKET_DATA_TYPE", "4"))

# Intervall des periodischen Portfolio-Snapshots (siehe write_portfolio_snapshot).
SNAPSHOT_INTERVAL_SEC = int(os.getenv("PTA_SNAPSHOT_INTERVAL_SEC", "30"))

# Ein leeres ib.portfolio() bei gleichzeitig vorhandenen Depotzeilen wird erst
# nach dieser Karenzzeit als "Konto ist wirklich flach" akzeptiert. Direkt nach
# dem Verbindungsaufbau ist der Portfolio-Cache noch nicht gefuellt — ohne diese
# Karenzzeit wuerde der erste Snapshot alle Positionen loeschen.
EMPTY_POSITIONS_CONFIRM_SEC = int(os.getenv("PTA_EMPTY_POSITIONS_CONFIRM_SEC", "60"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "http://gateway:80")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", os.getenv("SERVICE_ROLE_KEY", "missing"))

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as e:
    logger.error(f"Failed to initialize Supabase client: {e}")
    supabase = None

ib = IB()

# State
active_trading_mode = "live"
current_host = IB_HOST_DEFAULT
current_port = IB_PORT_DEFAULT

# Monotoner Zeitstempel des letzten Snapshot-Versuchs (fuer die Intervallsteuerung).
_last_snapshot_at = 0.0

# Seit wann ib.portfolio() durchgehend leer ist (None = nicht leer).
_empty_positions_since = None

# --- Database & Config Sync ---
def load_gateway_config():
    try:
        response = supabase.table("system_settings").select("value").eq("key", "ib_gateway_config").execute()
        if response.data:
            cfg = response.data[0].get("value", {})
            mode = "paper" if cfg.get("active_mode") == "paper" else "live"
            gateway_info = cfg.get(mode, {})
            return {
                "host": gateway_info.get("host", IB_HOST_DEFAULT),
                "port": int(gateway_info.get("port", IB_PORT_DEFAULT)),
                "mode": mode
            }
    except Exception as e:
        logger.warning(f"Could not load gateway config from DB: {e}")
    return {"host": IB_HOST_DEFAULT, "port": IB_PORT_DEFAULT, "mode": "live"}

def update_gateway_status(connected: bool):
    try:
        supabase.table("system_settings").upsert({
            "key": "ib_gateway_status",
            "value": {"connected": connected},
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        logger.info(f"Updated gateway status in DB to connected: {connected}")
    except Exception as e:
        logger.error(f"Exception during gateway status update: {e}")


# --- Event Queues for Callbacks ---
fill_events = []
status_events = []
commission_events = []

# --- Event Handlers for Callbacks ---

def on_connected():
    logger.info(f"Connected to IB Gateway ({active_trading_mode}).")
    def _run():
        update_gateway_status(True)
    threading.Thread(target=_run).start()
    ib.reqMarketDataType(IB_MARKET_DATA_TYPE)

def on_disconnected():
    logger.warning("Disconnected from IB Gateway.")
    def _run():
        update_gateway_status(False)
    threading.Thread(target=_run).start()

def on_exec_details(trade, fill):
    logger.info(f"Fill received: {fill.execution.execId} | {trade.contract.symbol} | {fill.execution.shares} @ {fill.execution.price}")
    action = "BUY" if fill.execution.side == "BOT" else "SELL"
    fill_events.append({
        "trade_id": fill.execution.orderRef or "UNKNOWN",
        "ticker": trade.contract.symbol,
        "action": action,
        "quantity": float(fill.execution.shares),
        "price": float(fill.execution.price),
        "broker_order_id": str(fill.execution.orderId),
        "broker_exec_id": fill.execution.execId,
        "currency": trade.contract.currency or "USD",
        "exchange": trade.contract.exchange or "SMART",
        "order_ref": fill.execution.orderRef or "UNKNOWN",
        # Capture the mode at fill time: a later mode switch must not relabel this fill.
        "mode": active_trading_mode
    })

def on_order_status(trade):
    logger.info(f"Order Status Update: OrderId {trade.order.orderId} | Status: {trade.orderStatus.status}")
    status_events.append({
        "order_id": trade.order.orderId,
        "status": trade.orderStatus.status,
        "trade_id": f"ORDER-{trade.order.orderId}",
        "ticker": trade.contract.symbol or "UNKNOWN",
        "why_held": trade.orderStatus.whyHeld or 'N/A',
        "mode": active_trading_mode
    })

def on_commission_report(trade, fill, report):
    logger.info(f"Commission Report received: ExecId {report.execId} | Commission: {report.commission} {report.currency}")
    commission_events.append({
        "exec_id": report.execId,
        "commission": float(report.commission),
        "currency": report.currency
    })

# Bind handlers
ib.connectedEvent += on_connected
ib.disconnectedEvent += on_disconnected
ib.execDetailsEvent += on_exec_details
ib.orderStatusEvent += on_order_status
ib.commissionReportEvent += on_commission_report


# --- Helper Methods ---

def parse_contract(ticker: str, currency: str, notes: str) -> Contract:
    try:
        if notes:
            notes_data = json.loads(notes)
            if notes_data.get('isOption'):
                # expiry expected in YYYYMMDD format
                contract = Option(
                    symbol=ticker, 
                    lastTradeDateOrContractMonth=notes_data['expiry'], 
                    strike=float(notes_data['strike']), 
                    right=notes_data['right'], 
                    exchange='SMART', 
                    currency=currency, 
                    multiplier=str(notes_data.get('multiplier', 100))
                )
                return contract
            elif notes_data.get('isCombo'):
                contract = Contract()
                contract.symbol = ticker
                contract.secType = 'BAG'
                contract.currency = currency
                contract.exchange = 'SMART'
                comboLegs = []
                for leg in notes_data.get('legs', []):
                    opt = Option(
                        symbol=ticker, 
                        lastTradeDateOrContractMonth=leg['expiry'], 
                        strike=float(leg['strike']), 
                        right=leg['right'], 
                        exchange='SMART', 
                        currency=currency
                    )
                    ib.qualifyContracts(opt) # We must synchronously qualify here to get conId
                    if getattr(opt, 'conId', 0) == 0:
                        raise ValueError(f"Could not qualify leg for {ticker} combo: {leg}")
                    l = ComboLeg(conId=opt.conId, ratio=leg['ratio'], action=leg['action'], exchange='SMART')
                    comboLegs.append(l)
                contract.comboLegs = comboLegs
                return contract
    except Exception as e:
        # Notes not JSON, or missing fields -> Fallback to stock
        pass
    
    return Stock(ticker, 'SMART', currency)


async def process_queued_events():
    global fill_events, status_events, commission_events
    
    if fill_events:
        fills = fill_events[:]
        fill_events = []
        for f in fills:
            try:
                await asyncio.to_thread(
                    lambda f=f: supabase.rpc("pta_log_event", {
                        "p_trade_id": f["trade_id"],
                        "p_ticker": f["ticker"],
                        "p_event_type": "FILL",
                        "p_action": f["action"],
                        "p_quantity": f["quantity"],
                        "p_price": f["price"],
                        "p_broker_order_id": f["broker_order_id"],
                        "p_broker_exec_id": f["broker_exec_id"],
                        "p_currency": f["currency"],
                        "p_exchange": f["exchange"],
                        "p_order_ref": f["order_ref"],
                        "p_mode": f["mode"]
                    }).execute()
                )
                logger.info(f"Processed FILL event for {f['broker_exec_id']} to DB.")
            except Exception as e:
                logger.error(f"Error processing fill event: {e}")

    if status_events:
        statuses = status_events[:]
        status_events = []
        for s in statuses:
            try:
                await asyncio.to_thread(
                    lambda s=s: supabase.table("pta_ibkr_open_orders").update({
                        "status": s["status"],
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }).eq("order_id", s["order_id"]).execute()
                )
                if s["status"] in ["Inactive", "Cancelled"]:
                    # p_take_profit + p_mode pin this call to exactly one overload: the
                    # one carrying p_take_profit + p_mode. With common args alone
                    # PostgREST cannot choose between the three pta_log_event overloads
                    # and fails with PGRST203 ("Could not choose the best candidate").
                    await asyncio.to_thread(
                        lambda s=s: supabase.rpc("pta_log_event", {
                            "p_trade_id": s["trade_id"],
                            "p_ticker": s["ticker"],
                            "p_event_type": "ORDER_STATUS_UPDATE",
                            "p_action": "INFO",
                            "p_broker_order_id": str(s["order_id"]),
                            "p_notes": f"Order changed status to {s['status']}. WhyHeld: {s['why_held']}",
                            "p_take_profit": None,
                            "p_mode": s["mode"]
                        }).execute()
                    )
            except Exception as e:
                logger.error(f"Error processing status event: {e}")

    if commission_events:
        comms = commission_events[:]
        commission_events = []
        for c in comms:
            try:
                # NOTE: pta_execution_log has no updated_at column. Sending it made
                # every commission update fail with PGRST204, so commissions were
                # silently never written (all daemon fills had commission 0).
                await asyncio.to_thread(
                    lambda c=c: supabase.table("pta_execution_log").update({
                        "commission": c["commission"],
                        "currency": c["currency"]
                    }).eq("broker_exec_id", c["exec_id"]).execute()
                )
            except Exception as e:
                logger.error(f"Error processing commission event: {e}")

# --- Main Sync Tasks ---

# --- Portfolio Snapshot ----------------------------------------------------
# pta_ibkr_positions / pta_ibkr_account_summary sind die EINZIGE Quelle, aus der
# Agenten den Depotstand lesen. Der Snapshot wird deshalb periodisch aus dem
# sync_loop geschrieben und nicht mehr nur auf Anfrage.
#
# Grund: ein anfragegetriebener Snapshot ist genau so frisch wie die letzte
# Anfrage — und wenn diese Anfrage unbemerkt scheitert, liefert das System
# stillschweigend einen eingefrorenen Stand als aktuellen Depotstand aus. Genau
# das ist passiert (siehe docs/architecture/broker-truth-snapshot.md).
#
# Der Refresh-auf-Anfrage-Pfad (handle_refresh_requests) bleibt fuer sofortige
# Aktualisierungen erhalten, ist aber nicht mehr die Grundlage der Frische.


async def write_portfolio_snapshot(reason: str) -> str:
    """Schreibt einen vollstaendigen Broker-Snapshot (Positionen, Kontostand, Orders).

    Rueckgabe "OK" bei Erfolg, sonst ein kurzer Grund-Code (NO_ACCOUNT_METRICS |
    FX_RATE_MISSING | EMPTY_POSITIONS_UNCONFIRMED | EXCEPTION). Der Aufrufer
    schreibt den Code in die Auftragszeile, damit ein Fehlschlag sichtbar wird
    statt still zu bleiben.

    Grundsatz: es wird NICHTS geschrieben, solange der neue Stand nicht
    vollstaendig vorliegt. Ein unvollstaendiger Snapshot darf einen guten nicht
    ueberschreiben — deshalb werden alle Daten zuerst aufgebaut und erst
    unmittelbar vor dem Schreiben geloescht.
    """
    try:
        
        # NOTE: kein ib.reqAccountUpdates() hier. Dieser ib_insync-Aufruf ist BLOCKING
        # (er wartet auf das Ende des accountValues-Downloads) und hat keinen Timeout;
        # aus dieser Schleife heraus friert er Order-Platzierung, Cancels und
        # Fill-Verarbeitung dauerhaft ein. ib.connect() abonniert bereits ueber
        # reqAccountUpdatesMultiAsync, der Streaming-Cache, den ib.portfolio() und
        # ib.accountValues() lesen, wird also frisch gehalten.
        positions = ib.portfolio()
        account_values = ib.accountValues()

        active_account_metrics = {}
        for val in account_values:
            acc = val.account
            if acc not in active_account_metrics:
                active_account_metrics[acc] = {"totalCashBalance": 0, "netLiquidation": 0, "availableFunds": 0}
            
            # Guard: some AccountValue entries carry non-numeric values (observed on the
            # paper account: an entry whose value is the account id itself). A bare
            # float() aborted the entire refresh, so no snapshot was ever written.
            try:
                num_val = float(val.value) if val.value else 0
            except (TypeError, ValueError):
                num_val = 0
            if val.currency in ["EUR", "BASE"]:
                if val.tag == "NetLiquidation":
                    active_account_metrics[acc]["netLiquidation"] = num_val
                elif val.tag == "AvailableFunds":
                    active_account_metrics[acc]["availableFunds"] = num_val
            
            if val.tag in ["TotalCashBalance", "TotalCashValue", "CashBalance"]:
                if val.currency == "BASE":
                    active_account_metrics[acc]["totalCashBalance"] = num_val
                elif val.currency == "EUR" and active_account_metrics[acc]["totalCashBalance"] == 0:
                    active_account_metrics[acc]["totalCashBalance"] = num_val

        # Guard: nur schreiben, wenn ein belastbarer Kontostand vorliegt. Direkt
        # nach dem Verbindungsaufbau sind die Account-Daten noch nicht geladen;
        # ohne diesen Guard wuerde ein solcher Lauf alle Depotzeilen loeschen und
        # durch leere/Null-Werte ersetzen.
        usable_metrics = {a: m for a, m in active_account_metrics.items() if m["netLiquidation"] > 0}
        if not usable_metrics:
            logger.warning(
                f"Snapshot ({reason}) uebersprungen: kein Konto mit NetLiquidation > 0 "
                f"({len(account_values)} AccountValues gelesen). Bestehender Snapshot bleibt unveraendert."
            )
            return "NO_ACCOUNT_METRICS"

        rates = {"EUR": 1.0}
        try:
            currencies = list(set([p.contract.currency for p in positions if p.contract.currency != "EUR"]))
            if currencies:
                fx_resp = await asyncio.to_thread(lambda: supabase.table("exchange_rates").select("*").in_("target_currency", currencies).eq("base_currency", "EUR").order("date", desc=True).execute())
                if fx_resp.data:
                    seen = set()
                    for row in fx_resp.data:
                        if row["target_currency"] not in seen:
                            rates[row["target_currency"]] = 1.0 / row["rate"]
                            seen.add(row["target_currency"])
        except Exception as e:
            logger.error(f"Failed to fetch exchange rates: {e}")

        # Vollstaendigkeit der FX-Kurse ist Pflicht: fehlt ein Kurs, wuerde
        # `rates.get(curr, 1.0)` den Marktwert 1:1 als EUR schreiben und den
        # gesamten Snapshot verfaelschen. Dann lieber gar nicht schreiben.
        missing_fx = sorted({p.contract.currency for p in positions if p.contract.currency not in rates})
        if missing_fx:
            logger.error(
                f"Snapshot ({reason}) uebersprungen: kein FX-Kurs (Basis EUR) fuer "
                f"{', '.join(missing_fx)}. Bestehender Snapshot bleibt unveraendert."
            )
            return "FX_RATE_MISSING"

        # ---- Erst aufbauen, dann schreiben --------------------------------
        # Zuvor wurde HIER geloescht, bevor die Folgeabfragen liefen. Eine
        # Exception weiter unten liess die Depotzeilen dann ohne Ersatz
        # verschwinden. Jetzt wird zuerst alles aufgebaut.
        existing = await asyncio.to_thread(lambda: supabase.table("pta_ibkr_positions").select("id").eq("mode", active_trading_mode).execute())
        existing_count = len(existing.data or [])

        global _empty_positions_since
        if positions:
            _empty_positions_since = None
        elif existing_count > 0:
            if _empty_positions_since is None:
                _empty_positions_since = time.monotonic()
            if time.monotonic() - _empty_positions_since < EMPTY_POSITIONS_CONFIRM_SEC:
                logger.warning(
                    f"Snapshot ({reason}) uebersprungen: ib.portfolio() ist leer, es liegen aber "
                    f"{existing_count} Depotzeile(n) vor. Direkt nach dem Verbindungsaufbau ist der "
                    f"Cache noch nicht gefuellt. Bestaetigt sich das {EMPTY_POSITIONS_CONFIRM_SEC}s "
                    f"lang, wird das Konto als flach uebernommen. Bestehender Snapshot bleibt unveraendert."
                )
                return "EMPTY_POSITIONS_UNCONFIRMED"
        
        total_heat = 0
        total_core_risk = 0

        inserts = []
        if positions:
            active_pos_data = await asyncio.to_thread(lambda: supabase.table("pta_active_positions").select("ticker, current_stop_loss").execute())
            stop_losses = {row["ticker"]: row["current_stop_loss"] for row in active_pos_data.data if row.get("current_stop_loss") is not None} if active_pos_data.data else {}

            for p in positions:
                acc = p.account
                net_liq = active_account_metrics.get(acc, {}).get("netLiquidation", 0)
                rate = rates.get(p.contract.currency, 1.0)
                mkt_val_eur = p.marketValue * rate
                pos_pct = (mkt_val_eur / net_liq) * 100 if net_liq > 0 else 0
                
                sl = stop_losses.get(p.contract.symbol)
                portfolio_heat_eur = 0
                core_risk_eur = 0
                
                if p.position > 0:
                    actual_sl = sl or 0
                    portfolio_heat_eur = (p.marketPrice - actual_sl) * p.position * rate
                    core_risk_eur = (p.averageCost - actual_sl) * p.position * rate
                elif p.position < 0:
                    actual_sl = sl or (p.marketPrice * 2)
                    portfolio_heat_eur = (actual_sl - p.marketPrice) * abs(p.position) * rate
                    core_risk_eur = (actual_sl - p.averageCost) * abs(p.position) * rate
                
                total_heat += portfolio_heat_eur
                total_core_risk += core_risk_eur

                inserts.append({
                    "account": acc,
                    "ticker": p.contract.symbol,
                    "currency": p.contract.currency,
                    "quantity": p.position,
                    "avg_cost": p.averageCost,
                    "market_price": p.marketPrice,
                    "market_value": p.marketValue,
                    "unrealized_pnl": p.unrealizedPNL,
                    "realized_pnl": p.realizedPNL,
                    "position_pct": pos_pct,
                    "portfolio_heat_eur": portfolio_heat_eur,
                    "core_risk_eur": core_risk_eur,
                    "mode": active_trading_mode,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            
        trades = ib.openTrades()
        order_inserts = []
        if trades:
            for t in trades:
                order_inserts.append({
                    "account": t.order.account or "UNKNOWN",
                    "perm_id": t.order.permId,
                    "order_id": t.order.orderId,
                    "ticker": t.contract.symbol,
                    "action": t.order.action,
                    "quantity": t.order.totalQuantity,
                    "order_type": t.order.orderType,
                    "limit_price": t.order.lmtPrice,
                    "stop_price": t.order.auxPrice,
                    "status": t.orderStatus.status,
                    "mode": active_trading_mode,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })

        # ---- Schreiben: delete+insert unmittelbar hintereinander -----------
        # Ab hier ist der neue Stand vollstaendig aufgebaut. Faellt eine der
        # folgenden Anweisungen aus, ist der Schaden auf diesen einen Schritt
        # begrenzt und nicht auf die vorgelagerten Abfragen.
        await asyncio.to_thread(lambda: supabase.table("pta_ibkr_positions").delete().eq("mode", active_trading_mode).execute())
        if inserts:
            await asyncio.to_thread(lambda: supabase.table("pta_ibkr_positions").insert(inserts).execute())

        # Nur Konten mit belastbarem Kontostand schreiben (siehe Guard oben).
        for acc, metrics in usable_metrics.items():
            cash_quote = (metrics["totalCashBalance"] / metrics["netLiquidation"]) * 100 if metrics["netLiquidation"] > 0 else 0
            await asyncio.to_thread(lambda m=metrics, a=acc, cq=cash_quote: supabase.table("pta_ibkr_account_summary").upsert({
                "account": a,
                "total_cash_balance": m["totalCashBalance"],
                "net_liquidation": m["netLiquidation"],
                "available_funds": m["availableFunds"],
                "cash_quote": cq,
                "portfolio_heat_eur": total_heat,
                "core_risk_eur": total_core_risk,
                "mode": active_trading_mode,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }, on_conflict="account,mode").execute())
        
        await asyncio.to_thread(lambda: supabase.table("pta_ibkr_open_orders").delete().eq("mode", active_trading_mode).execute())
        if order_inserts:
            await asyncio.to_thread(lambda: supabase.table("pta_ibkr_open_orders").insert(order_inserts).execute())

        nav_total = sum(m["netLiquidation"] for m in usable_metrics.values())
        logger.info(
            f"Snapshot ({reason}) geschrieben: {len(positions)} Position(en), "
            f"{len(trades)} offene Order(s), NAV {nav_total:.2f} EUR."
        )
        return "OK"

    except Exception as e:
        logger.error(f"Error writing portfolio snapshot ({reason}): {e}")
        return "EXCEPTION"


async def handle_refresh_requests():
    """Sofortiger Snapshot auf Anfrage — Ergaenzung zum periodischen Snapshot.

    Die Auswahl erfolgt bewusst ueber einen EXPLIZITEN notes-Zustand. Zuvor
    filterte dieser Pfad mit `notes != 'PROCESSING'`. In SQL ist
    `NULL <> 'PROCESSING'` jedoch NULL und damit nicht wahr — jede Zeile ohne
    notes fiel stillschweigend durch den Filter. Da pta_execution_log.notes
    keinen DB-Default hat, traf das auf JEDEN eingehenden Refresh-Auftrag zu:
    der Refresh lief nie an und die Agenten lieferten eingefrorene Depotwerte
    aus. Zeilen ohne notes werden daher zusaetzlich ueber einen expliziten
    NULL-Test eingesammelt.
    """
    try:
        pending = await asyncio.to_thread(lambda: supabase.table("pta_execution_log")
            .select("id")
            .eq("event_type", "REFRESH_REQUESTED")
            .eq("mode", active_trading_mode)
            .eq("notes", "PENDING")
            .execute())
        legacy = await asyncio.to_thread(lambda: supabase.table("pta_execution_log")
            .select("id")
            .eq("event_type", "REFRESH_REQUESTED")
            .eq("mode", active_trading_mode)
            .is_("notes", "null")
            .execute())

        ids = [r["id"] for r in (pending.data or [])] + [r["id"] for r in (legacy.data or [])]
        if not ids:
            return

        logger.info(f"Received {len(ids)} refresh request(s) for mode '{active_trading_mode}'. Writing snapshot...")
        await asyncio.to_thread(lambda: supabase.table("pta_execution_log").update({"notes": "PROCESSING"}).in_("id", ids).execute())

        code = await write_portfolio_snapshot("refresh_request")

        # Zustand zurueckschreiben, damit der Aufrufer Erfolg und Fehlschlag
        # unterscheiden kann — und im Fehlerfall den Grund sieht.
        final_notes = "COMPLETED" if code == "OK" else f"ERROR: {code}"
        await asyncio.to_thread(lambda n=final_notes, r=ids: supabase.table("pta_execution_log").update({
            "notes": n
        }).in_("id", r).execute())
    except Exception as e:
        logger.error(f"Error handling refresh requests: {e}")

async def handle_orders():
    try:
        # Mode isolation (CRITICAL): only pick up orders belonging to the active mode.
        # Without this filter an unconfirmed LIVE order would be sent to the paper
        # gateway (and, worse, a stale PAPER order to the live gateway = real money).
        resp = await asyncio.to_thread(lambda: supabase.table("pta_execution_log").select("*").eq("event_type", "ORDER_SUBMITTED").eq("mode", active_trading_mode).is_("broker_order_id", "null").execute())
        orders = resp.data
        if not orders:
            return

        for po in orders:
            if po.get("action") in ["DEPOSIT", "WITHDRAW"]:
                continue

            # Fail-safe whitelist. Below, an unrecognised action used to fall through
            # to order_action = "BUY", so a stray ORDER_SUBMITTED row (e.g. the
            # unhandled place_trade action "REFRESH") would have bought stock.
            if po.get("action") not in ["BUY", "SELL", "UPDATE"]:
                logger.error(f"Refusing order {po['id']} for {po.get('ticker')}: unknown action '{po.get('action')}'")
                await asyncio.to_thread(lambda id=po["id"]: supabase.table("pta_execution_log").update({
                    "notes": "ERROR: UNKNOWN_ACTION",
                    "broker_order_id": "FAILED"
                }).eq("id", id).execute())
                continue

            ticker = po.get("ticker")
            quantity = po.get("quantity")
            price = po.get("price")
            stop_price = po.get("stop_price")
            take_profit = po.get("take_profit")
            po_action = po.get("action")
            notes = po.get("notes") or ""

            is_bracket = (take_profit is not None or stop_price is not None) and po_action != "UPDATE"
            order_action = "BUY"
            if po_action in ["BUY", "SELL"]:
                order_action = po_action
            elif po_action == "UPDATE":
                pos_resp = await asyncio.to_thread(lambda t=ticker: supabase.table("pta_ibkr_positions").select("quantity").eq("ticker", t).execute())
                if pos_resp.data and pos_resp.data[0].get("quantity", 0) < 0:
                    order_action = "BUY"
                else:
                    order_action = "SELL"

            currency = po.get("currency") or "USD"
            contract = parse_contract(ticker, currency, notes)
            
            if contract.secType != 'BAG':
                ib.qualifyContracts(contract)
                if not getattr(contract, 'conId', 0):
                    logger.error(f"Failed to qualify contract for {ticker}")
                    await asyncio.to_thread(lambda id=po["id"]: supabase.table("pta_execution_log").update({
                        "notes": "ERROR: INVALID_CONTRACT",
                        "broker_order_id": "FAILED"
                    }).eq("id", id).execute())
                    continue

            if is_bracket:
                orders_to_place = []
                
                # Parent Order
                if price:
                    parent = LimitOrder(order_action, quantity, price)
                else:
                    parent = MarketOrder(order_action, quantity)
                parent.orderId = ib.client.getReqId()
                parent.transmit = False
                parent.orderRef = po.get("trade_id", "")
                orders_to_place.append(parent)
                
                # Take Profit Leg
                if take_profit:
                    tp = LimitOrder("SELL" if order_action == "BUY" else "BUY", quantity, take_profit)
                    tp.orderId = ib.client.getReqId()
                    tp.parentId = parent.orderId
                    tp.transmit = False
                    tp.orderRef = po.get("trade_id", "")
                    orders_to_place.append(tp)
                    
                # Stop Loss Leg
                if stop_price:
                    sl = StopOrder("SELL" if order_action == "BUY" else "BUY", quantity, stop_price)
                    sl.orderId = ib.client.getReqId()
                    sl.parentId = parent.orderId
                    sl.transmit = False
                    sl.orderRef = po.get("trade_id", "")
                    orders_to_place.append(sl)

                # Ensure only the last leg transmits the whole bracket
                if orders_to_place:
                    orders_to_place[-1].transmit = True
                
                # Opt-in extended-hours trading: a US order placed outside 09:30-16:00 ET
                # is otherwise only held and never executed. Activated by adding the
                # marker OUTSIDE_RTH to the order notes, so normal orders are unaffected.
                outside_rth = "OUTSIDE_RTH" in notes.upper()

                for o in orders_to_place:
                    if outside_rth:
                        o.outsideRth = True
                    ib.placeOrder(contract, o)
                
                current_order_id = parent.orderId

            else:
                if price and stop_price:
                    order = StopOrder(order_action, quantity, stop_price)
                    order.orderType = "STP LMT"
                    order.lmtPrice = price
                elif stop_price:
                    if "limit" in notes.lower():
                        order = StopOrder(order_action, quantity, stop_price)
                        order.orderType = "STP LMT"
                        order.lmtPrice = stop_price
                    else:
                        order = StopOrder(order_action, quantity, stop_price)
                elif price:
                    order = LimitOrder(order_action, quantity, price)
                else:
                    order = MarketOrder(order_action, quantity)
                
                order.orderRef = po.get("trade_id", "")
                if "OUTSIDE_RTH" in notes.upper():
                    order.outsideRth = True
                ib.placeOrder(contract, order)
                current_order_id = order.orderId

            await asyncio.to_thread(lambda id=po["id"], oid=current_order_id: supabase.table("pta_execution_log").update({
                "broker_order_id": str(oid)
            }).eq("id", id).execute())
            
            logger.info(f"Placed order for {ticker}: broker_order_id {current_order_id}")

    except Exception as e:
        logger.error(f"Error handling orders: {e}")

async def handle_cancels():
    try:
        # Mode isolation (CRITICAL): never cancel orders of the other mode's book.
        resp = await asyncio.to_thread(lambda: supabase.table("pta_execution_log").select("*").eq("event_type", "CANCEL_REQUESTED").eq("mode", active_trading_mode).is_("broker_order_id", "null").execute())
        reqs = resp.data
        if not reqs:
            return

        for cr in reqs:
            ticker = cr.get("ticker")
            notes = cr.get("notes") or ""
            target_perm_id = None
            if "PERM_ID:" in notes:
                try:
                    target_perm_id = int(notes.split("PERM_ID:")[1].strip())
                except:
                    pass
            
            open_trades = ib.openTrades()
            cancelled_count = 0
            for t in open_trades:
                if t.contract.symbol == ticker:
                    if target_perm_id and t.order.permId != target_perm_id:
                        continue
                    ib.cancelOrder(t.order)
                    cancelled_count += 1
            
            await asyncio.to_thread(lambda id=cr["id"], c=cancelled_count, t=ticker: supabase.table("pta_execution_log").update({
                "broker_order_id": "CANCELLED",
                "notes": f"Cancelled {c} order(s) for {t}"
            }).eq("id", id).execute())
            
            logger.info(f"Processed Cancel Request for {ticker}: cancelled {cancelled_count} orders.")

    except Exception as e:
        logger.error(f"Error handling cancels: {e}")

async def handle_quotes():
    try:
        resp = await asyncio.to_thread(lambda: supabase.table("pta_execution_log").select("*").eq("event_type", "QUOTE_REQUESTED").eq("notes", "PENDING").execute())
        reqs = resp.data
        if not reqs:
            return

        for qr in reqs:
            ticker = qr.get("ticker")
            if not ticker: continue
            
            await asyncio.to_thread(lambda id=qr["id"]: supabase.table("pta_execution_log").update({"notes": "PROCESSING"}).eq("id", id).execute())
            
            contract = Stock(ticker, 'SMART', qr.get("currency") or "USD")
            ib.qualifyContracts(contract)
            
            tickers = ib.reqTickers(contract)
            price = 0
            if tickers:
                t = tickers[0]
                price = t.marketPrice() or t.last or t.close or 0
            
            # NOTE: pta_execution_log has no updated_at column — sending it made every
            # quote completion fail with PGRST204, which is why get_quote always fell
            # back to Yahoo Finance instead of the IBKR price.
            await asyncio.to_thread(lambda id=qr["id"], p=price: supabase.table("pta_execution_log").update({
                "price": p,
                "notes": "COMPLETED"
            }).eq("id", id).execute())
            logger.info(f"Processed Quote for {ticker}: {price}")

    except Exception as e:
        logger.error(f"Error handling quotes: {e}")

# --- HTTP API ---
async def handle_search(request):
    query = request.query.get('q', '').strip()
    if not query:
        return web.json_response({"error": "Missing query parameter 'q'"}, status=400)
    if not ib.isConnected():
        return web.json_response({"error": "IBKR not connected"}, status=503)
    
    try:
        results = await ib.reqMatchingSymbolsAsync(query)
        candidates = []
        for desc in results:
            if desc.contract.secType in ['STK', 'ETF']:
                candidates.append({
                    "symbol": desc.contract.symbol,
                    "exchange": desc.contract.primaryExchange,
                    "secType": desc.contract.secType,
                    "currency": desc.contract.currency
                })
        return web.json_response({"results": candidates})
    except Exception as e:
        logger.error(f"Search API error: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def start_http_server():
    app = web.Application()
    app.router.add_get('/search', handle_search)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8005)
    await site.start()
    logger.info("HTTP Server running on port 8005")

async def sync_loop():
    global active_trading_mode, current_host, current_port, _last_snapshot_at
    
    await start_http_server()
    
    while True:
        try:
            # Process events collected by callbacks
            await process_queued_events()

            cfg = await asyncio.to_thread(load_gateway_config)
            new_mode = cfg["mode"]
            new_host = cfg["host"]
            new_port = cfg["port"]

            if new_mode != active_trading_mode or new_host != current_host or new_port != current_port:
                logger.info(f"Mode switch detected: {active_trading_mode} -> {new_mode} ({new_host}:{new_port})")
                active_trading_mode = new_mode
                current_host = new_host
                current_port = new_port
                
                if ib.isConnected():
                    ib.disconnect()
                
                await asyncio.sleep(2)
            
            if not ib.isConnected():
                logger.info(f"Connecting to IB Gateway at {current_host}:{current_port} (Mode: {active_trading_mode})...")
                try:
                    # ib.connect() is blocking but bounded, and it already performs the
                    # account/portfolio/executions sync (reqAccountUpdatesMultiAsync).
                    # Do NOT add ib.reqAccountUpdates() here: that call is unbounded-
                    # blocking and would freeze this loop permanently (see the note in
                    # handle_refresh_requests).
                    ib.connect(current_host, current_port, clientId=10002)
                except Exception as e:
                    logger.error(f"Connection failed: {e}")
                    await asyncio.sleep(5)
                    continue

            # Periodischer Portfolio-Snapshot: haelt die Depotwerte dauerhaft auf
            # Broker-Stand, unabhaengig davon, ob ein Agent danach fragt. Dadurch
            # ist list_active_positions ein reiner Lesezugriff auf eine Quelle,
            # die nie aelter als SNAPSHOT_INTERVAL_SEC ist.
            if time.monotonic() - _last_snapshot_at >= SNAPSHOT_INTERVAL_SEC:
                code = await write_portfolio_snapshot("periodic")
                if code == "OK":
                    _last_snapshot_at = time.monotonic()
                else:
                    # Einen Fehlschlag nicht ein ganzes Intervall lang
                    # quittieren, sonst verzoegert sich die Heilung.
                    _last_snapshot_at = time.monotonic() - SNAPSHOT_INTERVAL_SEC + 5

            await handle_refresh_requests()
            await handle_orders()
            await handle_cancels()
            await handle_quotes()

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Error in sync loop: {e}")
            await asyncio.sleep(2)

if __name__ == "__main__":
    if not supabase:
        logger.error("Supabase client not initialized. Exiting.")
        exit(1)
        
    try:
        util.patchAsyncio()
        asyncio.run(sync_loop())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        if ib.isConnected():
            ib.disconnect()
