#!/usr/bin/env bash
# Phase 2: exakter Upload-Datum-Backfill (kein Transkript-/Embedding-Zugriff).
# STEP=collect  -> holt video_id|upload_date fuer alle Videos ohne exaktes Datum, schreibt $OUT/all.tsv
# STEP=apply    -> laedt all.tsv und schreibt upload_date ; DRY_RUN=1 default
set -euo pipefail
CCO="llm-gw-mcp-cco"; DB="openbrain-db"
OUT="${OUT:-/home/daniel/QJM/dsh_playground/yt_phase2}"
PARALLEL="${PARALLEL:-8}"; STEP="${STEP:-collect}"; DRY_RUN="${DRY_RUN:-1}"; LIMIT="${LIMIT:-0}"
mkdir -p "$OUT"
psql_q(){ docker exec "$DB" psql -U postgres -d postgres -At -c "$1"; }

collect() {
  rm -f "$OUT"/all.tsv "$OUT"/part_*.tsv "$OUT"/batch_* 
  psql_q "select video_id from yt_videos where upload_date is null or upload_date_source is distinct from 'exact' order by video_id" > "$OUT/ids.txt"
  if [ "$LIMIT" != "0" ]; then head -n "$LIMIT" "$OUT/ids.txt" > "$OUT/ids.tmp"; mv "$OUT/ids.tmp" "$OUT/ids.txt"; fi
  echo "Exakt-Abfrage fuer $(wc -l < "$OUT/ids.txt") Videos, PARALLEL=$PARALLEL" >&2
  awk -v n="$PARALLEL" '{print > ("'"$OUT"'/batch_" (NR % n))}' "$OUT/ids.txt"
  pids=()
  for f in "$OUT"/batch_*; do
    ( docker exec -i "$CCO" yt-dlp --cookies /app/cookies.txt --batch-file /dev/stdin \
        --skip-download --no-warnings --print "%(id)s|%(upload_date)s" < "$f" 2>/dev/null \
      | grep -E '^[A-Za-z0-9_-]{11}\|' | awk -F'|' 'length($2)==8 {print $1"|"$2}' > "$OUT/part_$(basename "$f").tsv" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || true; done
  cat "$OUT"/part_*.tsv > "$OUT/all.tsv"
  echo "==> $(wc -l < "$OUT/all.tsv") exakte Daten in $OUT/all.tsv" >&2
}

apply() {
  [ -f "$OUT/all.tsv" ] || { echo "all.tsv fehlt - erst STEP=collect" >&2; exit 2; }
  docker cp "$OUT/all.tsv" "$DB:/tmp/yt_exact.tsv"
  update=""
  if [ "$DRY_RUN" = "0" ]; then
    update="UPDATE yt_videos v SET
      upload_date = to_date(t.upload_date,'YYYYMMDD'),
      upload_date_source = 'exact',
      upload_date_precision = 'day',
      metadata_synced_at = now()
    FROM tmp_exact t
    WHERE v.video_id = t.video_id
      AND (v.upload_date IS DISTINCT FROM to_date(t.upload_date,'YYYYMMDD')
           OR v.upload_date_source IS DISTINCT FROM 'exact');"
  fi
  docker exec -i "$DB" psql -U postgres -d postgres <<SQL
CREATE TEMP TABLE tmp_exact(video_id text PRIMARY KEY, upload_date text);
COPY tmp_exact(video_id, upload_date) FROM '/tmp/yt_exact.tsv' WITH (FORMAT text, DELIMITER '|');
\echo '--- Abgleich ---'
SELECT count(*) AS gelistet,
       count(v.video_id) AS in_db,
       count(*) FILTER (WHERE v.upload_date IS NULL) AS war_ohne_datum
FROM tmp_exact t LEFT JOIN yt_videos v USING (video_id);
\echo '--- Update (nur DRY_RUN=0) ---'
$update
SQL
}

if [ "$STEP" = "collect" ]; then collect; else apply; fi
