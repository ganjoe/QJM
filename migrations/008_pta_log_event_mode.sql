-- 008_pta_log_event_mode.sql
--
-- Problem: the sync daemon (ibkr_live_daemon/main.py) logs fills through the
-- pta_log_event overload that carries p_broker_exec_id + p_order_ref (needed for
-- the ON CONFLICT idempotency on idx_pta_unique_fill). That overload had NO
-- p_mode parameter and did not insert the `mode` column, so EVERY fill it wrote
-- fell back to the column default 'live' — including fills executed on the
-- IBKR paper account. A paper fill therefore never closed its paper position and
-- polluted the live ledger.
--
-- Fix: add p_mode (default 'live', so all existing callers stay compatible) to
-- that overload and insert it. CREATE OR REPLACE cannot change a signature, so
-- the old 15-argument function is dropped first. All other overloads
-- (p_take_profit / p_take_profit+p_mode) are untouched; every existing caller
-- disambiguates by its named arguments.
--
-- Verified before applying: pg_depend shows no dependent objects on the dropped OID.

BEGIN;

DROP FUNCTION IF EXISTS public.pta_log_event(
  text, text, text, text,
  numeric, numeric, numeric,
  text, numeric, text, text, numeric,
  text, text, text
);

CREATE FUNCTION public.pta_log_event(
  p_trade_id text,
  p_ticker text,
  p_event_type text,
  p_action text,
  p_quantity numeric DEFAULT NULL,
  p_price numeric DEFAULT NULL,
  p_stop_price numeric DEFAULT NULL,
  p_broker_order_id text DEFAULT NULL,
  p_commission numeric DEFAULT 0.0,
  p_currency text DEFAULT 'USD',
  p_exchange text DEFAULT NULL,
  p_slippage numeric DEFAULT 0.0,
  p_notes text DEFAULT NULL,
  p_broker_exec_id text DEFAULT NULL,
  p_order_ref text DEFAULT NULL,
  p_mode text DEFAULT 'live'
)
RETURNS bigint
LANGUAGE plpgsql
AS $function$
DECLARE
  v_id BIGINT;
BEGIN
  INSERT INTO pta_execution_log (
    trade_id, ticker, event_type, action, quantity, price, stop_price,
    broker_order_id, commission, currency, exchange, slippage, notes,
    broker_exec_id, order_ref, mode
  ) VALUES (
    p_trade_id, p_ticker, p_event_type, p_action, p_quantity, p_price, p_stop_price,
    p_broker_order_id, p_commission, p_currency, p_exchange, p_slippage, p_notes,
    p_broker_exec_id, COALESCE(p_order_ref, p_trade_id), COALESCE(p_mode, 'live')
  )
  ON CONFLICT (broker_exec_id) WHERE event_type = 'FILL' AND broker_exec_id IS NOT NULL
  DO NOTHING
  RETURNING id INTO v_id;

  IF v_id IS NULL THEN
    SELECT id INTO v_id
      FROM pta_execution_log
     WHERE broker_exec_id = p_broker_exec_id
     LIMIT 1;
  END IF;

  RETURN v_id;
END;
$function$;

COMMENT ON FUNCTION public.pta_log_event(
  text, text, text, text,
  numeric, numeric, numeric,
  text, numeric, text, text, numeric,
  text, text, text, text
) IS 'Sync-writer entry point: writes broker_exec_id + order_ref + mode. p_mode defaults to live for backward compatibility.';

-- REQUIRED: PostgREST caches function signatures. Without a schema reload it keeps
-- serving the dropped 15-argument overload and every sync fill fails with
-- PGRST202 ("Searched for the function ... but no match"). The NOTIFY is emitted
-- inside the transaction and delivered on COMMIT.
NOTIFY pgrst, 'reload schema';

COMMIT;
