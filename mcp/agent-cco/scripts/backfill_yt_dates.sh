#!/usr/bin/env bash
# (SUPERSEDED) Backfill yt_videos.upload_date WITHOUT re-downloading transcripts or embeddings.
# Fuer den exakten Lauf siehe phase2c_slow_upgrade.sh; approximativ: phase2b_approx_dates.sh.
#
# Modes:
#   MODE=approximate (default) - one flat-playlist call per channel, fast (~1 min),
#                                dates are coarse for old videos ("3 years ago").
#   MODE=exact                 - per-video metadata fetch (--skip-download), ~1.2 s/video,
#                                exact upload_date. Parallel via PARALLEL (default 8).
#
# Safety: only fills rows where upload_date IS NULL. Never overwrites existing dates.
#         DRY_RUN=1 (default) only reports; DRY_RUN=0 performs the UPDATE.
#
# Usage:  MODE=exact DRY_RUN=0 PARALLEL=8 ./backfill_yt_dates.sh
set -euo pipefail
CCO="llm-gw-mcp-cco"; DB="openbrain-db"; OUT="/tmp/yt_backfill"
MODE="${MODE:-approximate}"; DRY_RUN="${DRY_RUN:-1}"; PARALLEL="${PARALLEL:-8}"; LIMIT="${LIMIT:-0}"
mkdir -p "$OUT"; rm -f "$OUT"/all.tsv "$OUT"/part_*.tsv "$OUT"/batch_*; : > "$OUT/all.tsv"
psql_q() { docker exec "$DB" psql -U postgres -d postgres -At -c "$1"; }

if [ "$MODE" = "approximate" ]; then
  channels=$(psql_q "select handle from yt_channels where is_active order by handle")
  for h in $channels; do
    docker exec "$CCO" yt-dlp --cookies /app/cookies.txt --skip-download --flat-playlist \
      --extractor-args "youtubetab:approximate_date" --print "%(id)s|%(upload_date)s" \
      "https://www.youtube.com/${h}/videos" 2>/dev/null \
      | grep -E '^[A-Za-z0-9_-]{11}\|' | awk -F'|' 'length($2)==8 {print $1"|"$2}' >> "$OUT/all.tsv" || true
  done
elif [ "$MODE" = "exact" ]; then
  psql_q "select video_id from yt_videos where upload_date is null order by video_id" > "$OUT/ids.txt"
  if [ "$LIMIT" != "0" ]; then head -n "$LIMIT" "$OUT/ids.txt" > "$OUT/ids.tmp"; mv "$OUT/ids.tmp" "$OUT/ids.txt"; fi
  echo "Exakt-Abfrage für $(wc -l < "$OUT/ids.txt") Videos mit $PARALLEL parallelen Workern ..." >&2
  awk -v n="$PARALLEL" '{print > ("'"$OUT"'/batch_" (NR % n))}' "$OUT/ids.txt"
  pids=()
  for f in "$OUT"/batch_*; do
    ( docker exec -i "$CCO" yt-dlp --cookies /app/cookies.txt --batch-file /dev/stdin \
        --skip-download --no-warnings --print "%(id)s|%(upload_date)s" < "$f" 2>/dev/null \
      | grep -E '^[A-Za-z0-9_-]{11}\|' | awk -F'|' 'length($2)==8 {print $1"|"$2}' > "$OUT/part_$(basename "$f").tsv" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || true; done
  cat "$OUT"/part_*.tsv >> "$OUT/all.tsv"
else
  echo "Unbekannter MODE=$MODE (erwartet: approximate|exact)" >&2; exit 2
fi

echo "==> $(wc -l < "$OUT/all.tsv") Videos mit ermitteltem Datum (MODE=$MODE)" >&2
docker cp "$OUT/all.tsv" "$DB:/tmp/yt_dates.tsv"
update_sql=""
if [ "$DRY_RUN" = "0" ]; then
  update_sql="UPDATE yt_videos v SET upload_date = to_date(t.upload_date,'YYYYMMDD'), upload_date_source='approximate', upload_date_precision='day' FROM tmp_yt_dates t WHERE v.video_id = t.video_id AND v.upload_date IS NULL;"
fi
docker exec -i "$DB" psql -U postgres -d postgres <<SQL
CREATE TEMP TABLE tmp_yt_dates(video_id text PRIMARY KEY, upload_date text);
COPY tmp_yt_dates(video_id, upload_date) FROM '/tmp/yt_dates.tsv' WITH (FORMAT text, DELIMITER '|');
\echo '--- Abgleich gegen yt_videos ---'
SELECT count(*) AS gelistete_videos, count(v.video_id) AS in_db,
       count(*) FILTER (WHERE v.video_id IS NULL) AS nicht_in_db,
       count(*) FILTER (WHERE v.upload_date IS NULL) AS davon_ohne_datum
FROM tmp_yt_dates t LEFT JOIN yt_videos v USING (video_id);
\echo '--- Update (nur bei DRY_RUN=0) ---'
$update_sql
SQL
echo "FERTIG (MODE=$MODE DRY_RUN=$DRY_RUN)"
