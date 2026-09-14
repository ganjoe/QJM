#!/usr/bin/env bash
# Phase 2d: inkrementeller exakter Datums-Upgrade (approximate -> exact).
# Verarbeitet kleine Batches, speichert nach jedem Batch -> sichtbarer Fortschritt, resumierbar.
set -euo pipefail
CCO="llm-gw-mcp-cco"; DB="openbrain-db"
OUT="${OUT:-/home/daniel/QJM/dsh_playground/yt_phase2d}"
BATCH="${BATCH:-400}"; PARALLEL="${PARALLEL:-3}"; SLEEP="${SLEEP:-0.8}"; MAXBATCHES="${MAXBATCHES:-30}"
COOKIE_OPT="--cookies /app/cookies.txt"; if [ "${NO_COOKIES:-0}" = "1" ]; then COOKIE_OPT=""; fi
mkdir -p "$OUT"
pending(){ docker exec "$DB" psql -U postgres -d postgres -At -c "select count(*) from yt_videos where upload_date_source is distinct from 'exact'"; }
for i in $(seq 1 "$MAXBATCHES"); do
  before=$(pending)
  if [ "$before" -eq 0 ]; then echo "ALLE EXAKT"; break; fi
  rm -f "$OUT"/batch_* "$OUT"/part_*.tsv "$OUT"/all.tsv
  docker exec "$DB" psql -U postgres -d postgres -At -c "select video_id from yt_videos where upload_date_source is distinct from 'exact' order by video_id limit $BATCH" > "$OUT/ids.txt"
  n=$(wc -l < "$OUT/ids.txt")
  awk -v k="$PARALLEL" '{print > ("'"$OUT"'/batch_" (NR % k))}' "$OUT/ids.txt"
  pids=()
  for f in "$OUT"/batch_*; do
    ( docker exec -i "$CCO" yt-dlp $COOKIE_OPT --batch-file /dev/stdin \
        --skip-download --no-warnings --sleep-requests "$SLEEP" \
        --print "%(id)s|%(upload_date)s" < "$f" 2>/dev/null \
      | grep -E '^[A-Za-z0-9_-]{11}\|' | awk -F'|' 'length($2)==8 {print $1"|"$2}' > "$OUT/part_$(basename "$f").tsv" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || true; done
  cat "$OUT"/part_*.tsv > "$OUT/all.tsv"
  ok=$(wc -l < "$OUT/all.tsv")
  docker cp "$OUT/all.tsv" "$DB:/tmp/yt_exact.tsv"
  docker exec -i "$DB" psql -U postgres -d postgres >/dev/null <<SQL
CREATE TEMP TABLE tmp_exact(video_id text PRIMARY KEY, upload_date text);
COPY tmp_exact(video_id, upload_date) FROM '/tmp/yt_exact.tsv' WITH (FORMAT text, DELIMITER '|');
UPDATE yt_videos v SET upload_date=to_date(t.upload_date,'YYYYMMDD'), upload_date_source='exact', upload_date_precision='day', metadata_synced_at=now()
FROM tmp_exact t WHERE v.video_id=t.video_id AND v.upload_date_source IS DISTINCT FROM 'exact';
SQL
  after=$(pending)
  echo "[$(date -u +%H:%M:%S)] Batch $i: $n angefragt, $ok erhalten, offen $before -> $after"
  gained=$((before - after))
  if [ "$gained" -le 0 ]; then echo "  kein Fortschritt -> 900s Pause"; sleep 900
  elif [ "$gained" -lt $((n / 10)) ]; then echo "  wenig Fortschritt ($gained/$n) -> 300s Pause"; sleep 300; fi
done
echo "PHASE2D ENDE"
