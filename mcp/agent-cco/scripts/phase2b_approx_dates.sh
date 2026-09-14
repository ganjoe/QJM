#!/usr/bin/env bash
# Phase 2b: approximative Zwischenschicht fuer noch unbekannte Upload-Daten.
# Schreibt nur in Zeilen mit upload_date IS NULL; exact bleibt unberuehrt.
set -euo pipefail
CCO="llm-gw-mcp-cco"; DB="openbrain-db"
OUT="${OUT:-/home/daniel/QJM/dsh_playground/yt_phase2b}"; DRY_RUN="${DRY_RUN:-1}"
mkdir -p "$OUT"; : > "$OUT/approx.tsv"
channels=$(docker exec "$DB" psql -U postgres -d postgres -At -c "select handle from yt_channels where is_active order by handle")
for h in $channels; do
  docker exec "$CCO" yt-dlp --cookies /app/cookies.txt --skip-download --flat-playlist \
    --extractor-args "youtubetab:approximate_date" --print "%(id)s|%(upload_date)s" \
    "https://www.youtube.com/${h}/videos" 2>/dev/null \
    | grep -E '^[A-Za-z0-9_-]{11}\|' | awk -F'|' 'length($2)==8 {print $1"|"$2}' >> "$OUT/approx.tsv" || true
done
docker cp "$OUT/approx.tsv" "$DB:/tmp/yt_approx.tsv"
update=""
if [ "$DRY_RUN" = "0" ]; then
  update="UPDATE yt_videos v SET upload_date=to_date(t.upload_date,'YYYYMMDD'), upload_date_source='approximate', upload_date_precision='day', metadata_synced_at=now() FROM tmp_approx t WHERE v.video_id=t.video_id AND v.upload_date IS NULL;"
fi
docker exec -i "$DB" psql -U postgres -d postgres <<SQL
CREATE TEMP TABLE tmp_approx(video_id text PRIMARY KEY, upload_date text);
COPY tmp_approx(video_id, upload_date) FROM '/tmp/yt_approx.tsv' WITH (FORMAT text, DELIMITER '|');
\echo '--- Abgleich ---'
SELECT count(*) AS gelistet, count(v.video_id) AS in_db,
       count(*) FILTER (WHERE v.upload_date IS NULL) AS davon_ohne_datum
FROM tmp_approx t LEFT JOIN yt_videos v USING (video_id);
\echo '--- Update ---'
$update
SQL
echo "FERTIG (DRY_RUN=$DRY_RUN)"
