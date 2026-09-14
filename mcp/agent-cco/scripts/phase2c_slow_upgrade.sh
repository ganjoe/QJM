#!/usr/bin/env bash
# Phase 2c: langsamer, wiederaufsetzbarer exakter Upgrade-Lauf.
# Ueberschreibt approximate -> exact, laesst exact unberuehrt, respektiert Rate-Limits.
set -euo pipefail
CCO="llm-gw-mcp-cco"; DB="openbrain-db"
OUT="${OUT:-/home/daniel/QJM/dsh_playground/yt_phase2c}"
PARALLEL="${PARALLEL:-2}"; SLEEP="${SLEEP:-2}"; MAXROUNDS="${MAXROUNDS:-60}"
mkdir -p "$OUT"
count_pending(){ docker exec "$DB" psql -U postgres -d postgres -At -c "select count(*) from yt_videos where upload_date_source is distinct from 'exact'"; }
for round in $(seq 1 "$MAXROUNDS"); do
  before=$(count_pending)
  echo "[$(date -u +%H:%M:%S)] Runde $round: $before offen"
  if [ "$before" -eq 0 ]; then echo "ALLE EXAKT"; break; fi
  rm -f "$OUT"/part_*.tsv "$OUT"/batch_*
  docker exec "$DB" psql -U postgres -d postgres -At -c "select video_id from yt_videos where upload_date_source is distinct from 'exact' order by video_id" > "$OUT/ids.txt"
  awk -v n="$PARALLEL" '{print > ("'"$OUT"'/batch_" (NR % n))}' "$OUT/ids.txt"
  pids=()
  for f in "$OUT"/batch_*; do
    ( docker exec -i "$CCO" yt-dlp --cookies /app/cookies.txt --batch-file /dev/stdin \
        --skip-download --no-warnings --sleep-requests "$SLEEP" \
        --print "%(id)s|%(upload_date)s" < "$f" 2>/dev/null \
      | grep -E '^[A-Za-z0-9_-]{11}\|' | awk -F'|' 'length($2)==8 {print $1"|"$2}' > "$OUT/part_$(basename "$f").tsv" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || true; done
  cat "$OUT"/part_*.tsv > "$OUT/all.tsv"
  docker cp "$OUT/all.tsv" "$DB:/tmp/yt_exact.tsv"
  docker exec -i "$DB" psql -U postgres -d postgres >/dev/null <<SQL
CREATE TEMP TABLE tmp_exact(video_id text PRIMARY KEY, upload_date text);
COPY tmp_exact(video_id, upload_date) FROM '/tmp/yt_exact.tsv' WITH (FORMAT text, DELIMITER '|');
UPDATE yt_videos v SET upload_date=to_date(t.upload_date,'YYYYMMDD'), upload_date_source='exact', upload_date_precision='day', metadata_synced_at=now()
FROM tmp_exact t WHERE v.video_id=t.video_id AND v.upload_date_source IS DISTINCT FROM 'exact';
SQL
  after=$(count_pending)
  gained=$((before - after))
  echo "[$(date -u +%H:%M:%S)] Runde $round: $before -> $after (exakt +$gained)"
  if [ "$gained" -le 0 ]; then echo "  kein Fortschritt -> warte 600s"; sleep 600
  elif [ "$gained" -lt $((before / 5)) ]; then echo "  wenig Fortschritt -> warte 180s"; sleep 180
  else sleep 20; fi
done
echo "PHASE2C ENDE"
