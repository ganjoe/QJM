-- 012_cda_master_universe_massive.sql
-- Extended metadata for the Massive provider universe sync.
-- All additive and idempotent.

ALTER TABLE public.cda_master_universe
    ADD COLUMN IF NOT EXISTS source        text,
    ADD COLUMN IF NOT EXISTS provider      text,
    ADD COLUMN IF NOT EXISTS type          text,
    ADD COLUMN IF NOT EXISTS active        boolean DEFAULT true,
    ADD COLUMN IF NOT EXISTS delisted_utc  timestamp with time zone;

CREATE INDEX IF NOT EXISTS cda_master_universe_has_parquet_idx
    ON public.cda_master_universe (has_parquet);
CREATE INDEX IF NOT EXISTS cda_master_universe_source_idx
    ON public.cda_master_universe (source);
