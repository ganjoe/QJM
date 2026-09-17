-- 009_backfill_crwv_enph_exits.sql
--
-- One-time ledger repair for two LIVE positions that were closed at the broker but
-- never recorded, because the sync daemon was down when the fills happened:
--
--   CRWV  entry trade_id 5246a1737e032371330917ad0b1d745c  BUY  3  @ 107.18 (2026-08-12)
--   ENPH  entry trade_id 9123179d2c668a2abc732a5bed3a6173  BUY 10  @  41.82 (2026-08-04)
--
-- On 2026-08-31 17:49 UTC the system itself submitted the exit orders
-- (ORDER_SUBMITTED SELL 3 / broker_order_id 61 and SELL 10 / broker_order_id 62) but
-- with the synthetic trade_id '<TICKER>-MANUAL-EXIT' instead of the entry trade_id.
-- The orders filled on 2026-09-01; no FILL row was ever written (live gateway down,
-- and ib.reqAccountUpdates() had already frozen the sync loop — see migration 008
-- and the daemon fix).
--
-- Consequence before this repair: pta_active_positions reported both as open longs
-- while the broker held neither -> the only two ledger/broker drifts in the book.
--
-- The SELL rows are inserted with the ORIGINAL entry trade_id so the FIFO ledger nets
-- to zero and the positions close. `mode` is 'live' — these are live-account trades.
--
-- NOTE ON DOUBLE COUNTING: broker_exec_id carries a BACKFILL-* marker so this repair
-- is idempotent (idx_pta_unique_fill) and traceable. If the September broker statement
-- is ever imported through ibkr_csv_importer, these two trades must be excluded until
-- that importer's cross-batch FIFO matching and execId de-duplication are fixed —
-- otherwise the same sale is booked twice.

BEGIN;

-- 1) Link the stale exit-order rows to the trade they actually belong to.
UPDATE pta_execution_log
   SET trade_id = '5246a1737e032371330917ad0b1d745c'
 WHERE trade_id = 'CRWV-MANUAL-EXIT';

UPDATE pta_execution_log
   SET trade_id = '9123179d2c668a2abc732a5bed3a6173'
 WHERE trade_id = 'ENPH-MANUAL-EXIT';

-- 2) Insert the two missing SELL fills, guarded:
--    - the position must still be open with exactly the quantity that was sold
--    - no SELL fill may already exist for that trade
INSERT INTO pta_execution_log
  (trade_id, ticker, event_type, action, quantity, price, commission,
   currency, exchange, mode, notes, created_at, broker_order_id, broker_exec_id)
SELECT v.trade_id, v.ticker, 'FILL', 'SELL', v.qty, v.price, v.commission,
       'USD', 'SMART', 'live', v.notes, v.ts, v.order_id, v.exec_id
FROM (VALUES
  ('5246a1737e032371330917ad0b1d745c', 'CRWV',   3, 82.28, 2.00,
   TIMESTAMPTZ '2026-09-01 15:30:00+00', '61', 'BACKFILL-20260901-CRWV',
   'Backfill: system exit order 61 (submitted 2026-08-31) filled at broker, FILL was never logged — sync daemon down'),
  ('9123179d2c668a2abc732a5bed3a6173', 'ENPH',  10, 35.66, 2.00,
   TIMESTAMPTZ '2026-09-01 15:30:00+00', '62', 'BACKFILL-20260901-ENPH',
   'Backfill: system exit order 62 (submitted 2026-08-31) filled at broker, FILL was never logged — sync daemon down')
) AS v(trade_id, ticker, qty, price, commission, ts, order_id, exec_id, notes)
WHERE EXISTS (
        SELECT 1 FROM pta_active_positions p
         WHERE p.trade_id = v.trade_id AND p.mode = 'live' AND p.net_quantity = v.qty)
  AND NOT EXISTS (
        SELECT 1 FROM pta_execution_log f
         WHERE f.trade_id = v.trade_id AND f.event_type = 'FILL'
           AND f.action = 'SELL' AND f.mode = 'live');

-- 3) Report what happened (must be 2 inserts' worth = 2 closed trades, 0 drift).
\echo '--- CRWV / ENPH trade state after repair ---'
SELECT trade_id, ticker, qty_bought, qty_sold, is_closed, is_winner,
       round(net_pnl, 2) AS net_pnl_usd, round(net_pnl_eur, 2) AS net_pnl_eur
  FROM pta_trade_performance
 WHERE ticker IN ('CRWV', 'ENPH')
 ORDER BY ticker;

\echo '--- ledger vs broker drift (live) — MUST BE EMPTY ---'
WITH ledger AS (
  SELECT ticker, sum(net_quantity) AS q FROM pta_active_positions WHERE mode = 'live' GROUP BY ticker),
     broker AS (
  SELECT ticker, sum(quantity)     AS q FROM pta_ibkr_positions  WHERE mode = 'live' GROUP BY ticker)
SELECT COALESCE(l.ticker, b.ticker) AS ticker, l.q AS ledger_qty, b.q AS broker_qty
  FROM ledger l FULL OUTER JOIN broker b ON l.ticker = b.ticker
 WHERE l.q IS DISTINCT FROM b.q;

COMMIT;
