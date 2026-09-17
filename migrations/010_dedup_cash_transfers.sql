-- 010_dedup_cash_transfers.sql
--
-- Removes duplicated CASH_TRANSFER rows (deposits, withdrawals and dividends).
--
-- Cause: ibkr_csv_importer/migrate_xml_to_pg.py::get_existing_ids() de-duplicated
-- only FILL rows ("event_type=eq.FILL"), while the XML id of a cash movement is
-- stored verbatim as trade_id. Every repeated CSV import therefore inserted each
-- deposit / withdrawal / dividend again. Every one of the 18 cash trade_ids was
-- present exactly twice:
--
--   deposits     28,138.46 EUR booked  ->  14,069.23 EUR actual
--   withdrawals   2,080.00 EUR booked  ->   1,040.00 EUR actual
--   net injected 26,058.46 EUR booked  ->  13,029.23 EUR actual
--
-- That double booking inflated pta_portfolio_summary.calculated_cash_balance_eur
-- by ~13,000 EUR and was the bulk of the ~19,500 EUR gap against the broker's
-- reported cash.
--
-- The importer itself is fixed in the same change set, so a re-import is now a
-- verified no-op. This migration repairs the rows already written.
--
-- Idempotent: keeps the lowest id per trade_id; a second run deletes nothing.
-- Rows affected before the fix: 18. Backup of the affected rows (all copies) was
-- taken to backups/duplicate_cash_transfers_20260917.csv before applying.

BEGIN;

DELETE FROM pta_execution_log
WHERE id IN (
  SELECT id FROM (
    SELECT id, row_number() OVER (PARTITION BY trade_id ORDER BY id) AS rn
      FROM pta_execution_log
     WHERE event_type = 'CASH_TRANSFER'
  ) t
  WHERE rn > 1
);

\echo '--- cash flow after de-duplication ---'
SELECT * FROM pta_cash_flow;

\echo '--- remaining duplicate cash trade_ids (must be 0) ---'
SELECT count(*) AS duplicate_cash_trade_ids FROM (
  SELECT trade_id FROM pta_execution_log
   WHERE event_type = 'CASH_TRANSFER'
   GROUP BY trade_id HAVING count(*) > 1
) x;

COMMIT;
