-- 006_scan_watchlist_meta.sql
-- Metadaten der automatisch gepflegten Scanner-Watchlist (z. B. 'scan_latest').
--
-- Der Universal Scanner schreibt sein komplettes Treffer-Ergebnis nach jedem Lauf in eine
-- rollierende Watchlist (pca_watchlists, list_name = 'scan_latest'). Diese Tabelle haelt fest,
-- WIE dieser Stand entstanden ist (Filter, Sortierung, Universum, Trefferzahl, Zeitpunkt),
-- damit der Agent den Inhalt erklaeren kann, ohne den Scan zu wiederholen.
--
-- run_seq dient als Sequence-Guard: bei parallelen Scans darf ein aelterer, langsamer Lauf
-- einen bereits geschriebenen neueren Stand nicht ueberschreiben.
--
--   matched_count   = vollstaendige Trefferzahl (KEIN Cap, alles steht in der Watchlist)
--   response_count  = Treffer, die die API-Response ausgeliefert hat (limit-gekappt)
--   truncated       = TRUE, wenn die Response durch 'limit' gekappt wurde
--
-- Hinweis: list_name ist bewusst nicht als FK auf pca_watchlists definiert, da eine leere
-- Watchlist (0 Treffer) keine Zeilen in pca_watchlists hat, aber dennoch einen Metadaten-
-- Eintrag mit matched_count = 0 besitzen soll.

CREATE TABLE IF NOT EXISTS pca_scan_watchlist_meta (
    list_name       TEXT PRIMARY KEY,
    scanner         TEXT NOT NULL DEFAULT 'universal_scanner',
    query_hash      TEXT,
    query_json      JSONB,
    matched_count   INTEGER NOT NULL DEFAULT 0,
    evaluated_count INTEGER NOT NULL DEFAULT 0,
    response_count  INTEGER NOT NULL DEFAULT 0,
    truncated       BOOLEAN NOT NULL DEFAULT FALSE,
    run_seq         BIGINT  NOT NULL DEFAULT 0,
    source          TEXT    NOT NULL DEFAULT 'universal_scanner',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE pca_scan_watchlist_meta IS
    'Metadaten der rollierenden Scanner-Watchlist (z. B. scan_latest): Filter, Trefferzahl und Zeitpunkt des letzten Scan-Laufs';

COMMENT ON COLUMN pca_scan_watchlist_meta.list_name IS
    'Name der Watchlist in pca_watchlists (Default: scan_latest)';

COMMENT ON COLUMN pca_scan_watchlist_meta.query_json IS
    'Roh-Query des Laufs (Filter/Expression/Sort/Universum) zur Nachvollziehbarkeit';

COMMENT ON COLUMN pca_scan_watchlist_meta.matched_count IS
    'Vollstaendige Trefferzahl des Laufs (ohne Cap) - entspricht der Zeilenzahl in pca_watchlists';

COMMENT ON COLUMN pca_scan_watchlist_meta.response_count IS
    'Anzahl Treffer in der API-Response (durch limit gekappt)';

COMMENT ON COLUMN pca_scan_watchlist_meta.run_seq IS
    'Monoton steigende Lauf-Sequenz; verhindert, dass ein aelterer Scan einen neueren Stand ueberschreibt';

CREATE INDEX IF NOT EXISTS idx_pca_scan_watchlist_meta_updated_at
    ON pca_scan_watchlist_meta (updated_at DESC);

-- PostgREST muss das neue Schema laden, sonst antwortet /rest/v1/pca_scan_watchlist_meta mit 404.
NOTIFY pgrst, 'reload schema';
