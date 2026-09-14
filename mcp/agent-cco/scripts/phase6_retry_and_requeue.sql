-- Retry-Unterstuetzung + Requeue der transient fehlgeschlagenen Videos
BEGIN;
ALTER TABLE yt_videos ADD COLUMN IF NOT EXISTS retry_count integer NOT NULL DEFAULT 0;
ALTER TABLE yt_videos ADD COLUMN IF NOT EXISTS next_retry_at timestamptz NOT NULL DEFAULT now();

-- Download-Fehler: Premieres + Rate-Limit -> wieder pending
UPDATE yt_videos SET status='pending', error_msg=NULL, retry_count=0, next_retry_at=now()
WHERE status='failed' AND (error_msg ILIKE '%Premieres in%' OR error_msg ILIKE '%try again later%');

-- Embedding-Fehler mit vorhandenem Transkript -> wieder downloaded
UPDATE yt_videos SET status='downloaded', error_msg=NULL, retry_count=0, next_retry_at=now()
WHERE status='failed' AND error_msg ILIKE '%Embeddings failed%' AND transcript IS NOT NULL;

SELECT status, count(*) FROM yt_videos GROUP BY status ORDER BY status;
-- WICHTIG: PostgREST muss die neuen Spalten kennen, sonst schlagen Worker-Updates
-- still mit PGRST204 fehl (Endlosschleife).
NOTIFY pgrst, 'reload schema';
COMMIT;
