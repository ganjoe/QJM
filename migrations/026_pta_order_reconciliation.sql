-- 026: Order-Reconciliation — Stop-Loeschung, Read-back, vollstaendiger Order-Snapshot
--
-- Behebt die im Report report-ibkr-order-reconciliation.md verifizierten Defekte
-- auf Datenbankebene. Die Code-Seite (ibkr_live_daemon/main.py, mcp/agent-pta)
-- setzt auf diesen Spalten/der View auf.
--
-- 1. pta_active_positions (VIEW) konnte einen geloeschten Stop nicht darstellen:
--    latest_stops filterte mit "stop_price IS NOT NULL". Ein Loeschversuch
--    (stop_loss = 0 -> NULL) fiel durch den Filter, und der ALTE Stop blieb
--    fuer immer "der aktuelle". Es gab damit ueberhaupt keine Repraesentation
--    fuer "Stop entfernt" — unabhaengig vom eingegebenen Wert.
--    Fix: eigenes Event STOP_REMOVED zaehlt als Stop-Ereignis und setzt den
--    aktuellen Stop explizit auf NULL.
--
-- 2. pta_execution_log hatte keine Read-back-Spalten. place_trade konnte daher
--    nur "Event logged" melden, auch wenn IB die Order Sekunden spaeter mit
--    Error 321 (size value cannot be zero) verwarf. Der Daemon schreibt den
--    Broker-Status jetzt an DIESE Zeilen zurueck.
--
-- 3. pta_ibkr_open_orders fehlten die Felder fuer einen echten Abgleich
--    (order_ref, client_id, tif, filled, remaining, parent_id).
--
-- Idempotent. NOTIFY pgrst am Ende, damit PostgREST die neuen Spalten sieht.

BEGIN;

-- 1. Read-back / Verifikation ------------------------------------------------
ALTER TABLE public.pta_execution_log
  ADD COLUMN IF NOT EXISTS broker_status      text,
  ADD COLUMN IF NOT EXISTS broker_error_code  integer,
  ADD COLUMN IF NOT EXISTS broker_error_msg   text,
  ADD COLUMN IF NOT EXISTS broker_verified_at timestamp with time zone;

COMMENT ON COLUMN public.pta_execution_log.broker_status IS
  'Letzter vom Broker gemeldeter Order-Status zu dieser Auftragszeile (PreSubmitted/Submitted/Filled/Cancelled/...). NULL = noch keine Rueckmeldung.';
COMMENT ON COLUMN public.pta_execution_log.broker_error_code IS
  'IB-Fehlercode der letzten Ablehnung (z. B. 321 = size value cannot be zero, 201 = Order rejected). NULL = kein Fehler gemeldet.';
COMMENT ON COLUMN public.pta_execution_log.broker_verified_at IS
  'Zeitpunkt der letzten Broker-Rueckmeldung zu dieser Zeile. NULL = unbeantwortet (nicht verifiziert).';

-- 2. Order-Snapshot vervollstaendigen ----------------------------------------
ALTER TABLE public.pta_ibkr_open_orders
  ADD COLUMN IF NOT EXISTS order_ref text,
  ADD COLUMN IF NOT EXISTS client_id integer,
  ADD COLUMN IF NOT EXISTS tif       text,
  ADD COLUMN IF NOT EXISTS filled    numeric,
  ADD COLUMN IF NOT EXISTS remaining numeric,
  ADD COLUMN IF NOT EXISTS parent_id integer;

-- 3. Stop-Loeschung in der Positions-View darstellbar machen ----------------
CREATE OR REPLACE VIEW public.pta_active_positions AS
 WITH fill_aggregation AS (
         SELECT pta_execution_log.trade_id,
            pta_execution_log.ticker,
            pta_execution_log.currency,
            pta_execution_log.mode,
            sum(
                CASE
                    WHEN pta_execution_log.action = ANY (ARRAY['BUY'::text, 'DEPOSIT'::text]) THEN pta_execution_log.quantity
                    WHEN pta_execution_log.action = ANY (ARRAY['SELL'::text, 'WITHDRAW'::text]) THEN - pta_execution_log.quantity
                    ELSE 0::numeric
                END) AS net_quantity,
            sum(pta_execution_log.commission) AS total_commission,
            sum(pta_execution_log.slippage) AS total_slippage,
            min(pta_execution_log.created_at) AS open_time
           FROM pta_execution_log
          WHERE pta_execution_log.event_type = 'FILL'::text
          GROUP BY pta_execution_log.trade_id, pta_execution_log.ticker, pta_execution_log.currency, pta_execution_log.mode
        ), stop_events AS (
         SELECT pta_execution_log.trade_id,
            pta_execution_log.mode,
            pta_execution_log.created_at,
            pta_execution_log.stop_price,
            pta_execution_log.event_type
           FROM pta_execution_log
          WHERE (pta_execution_log.event_type = 'ORDER_SUBMITTED'::text AND pta_execution_log.stop_price IS NOT NULL)
             OR pta_execution_log.event_type = 'STOP_REMOVED'::text
        ), latest_stops AS (
         SELECT DISTINCT ON (stop_events.trade_id, stop_events.mode) stop_events.trade_id,
            stop_events.mode,
                CASE
                    WHEN stop_events.event_type = 'STOP_REMOVED'::text THEN NULL::numeric
                    ELSE stop_events.stop_price
                END AS current_stop_loss
           FROM stop_events
          ORDER BY stop_events.trade_id, stop_events.mode, stop_events.created_at DESC
        )
 SELECT f.trade_id,
    f.ticker,
    f.currency,
    f.mode,
    f.net_quantity,
    f.total_commission,
    f.total_slippage,
    f.open_time,
    s.current_stop_loss,
        CASE
            WHEN f.net_quantity > 0::numeric THEN 'LONG'::text
            WHEN f.net_quantity < 0::numeric THEN 'SHORT'::text
            ELSE 'CLOSED'::text
        END AS position_type
   FROM fill_aggregation f
     LEFT JOIN latest_stops s ON f.trade_id = s.trade_id AND f.mode = s.mode
  WHERE f.net_quantity <> 0::numeric;

COMMENT ON VIEW public.pta_active_positions IS
  'Offene Positionen je trade_id, aggregiert aus FILL-Events. current_stop_loss = letztes Stop-Ereignis (ORDER_SUBMITTED mit stop_price ODER STOP_REMOVED). ACHTUNG: das ist die Stop-ABSICHT aus dem Ledger, kein Nachweis einer arbeitenden Broker-Order — dafuer pta_ibkr_open_orders heranziehen.';

NOTIFY pgrst, 'reload schema';

COMMIT;
