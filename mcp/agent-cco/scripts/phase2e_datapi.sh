#!/usr/bin/env bash
# Phase 2e: exakter Upload-Datum-Backfill ueber YouTube Data API v3 (50 IDs/Request).
# Voraussetzung: API-Key mit Zugriff auf YouTube Data API v3.
# Key-Quelle: $YOUTUBE_API_KEY oder GEMINI_API_KEY aus llm-gw-dashboard.
set -euo pipefail
CCO="llm-gw-mcp-cco"; DB="openbrain-db"
OUT="${OUT:-/home/daniel/QJM/dsh_playground/yt_phase2e}"
KEY="${YOUTUBE_API_KEY:-$(docker exec llm-gw-dashboard printenv GEMINI_API_KEY 2>/dev/null | tr -d '\r')}"
BATCH="${BATCH:-50}"
mkdir -p "$OUT"; rm -f "$OUT"/ids.txt "$OUT"/chunk_* "$OUT"/all.tsv
pending(){ docker exec "$DB" psql -U postgres -d postgres -At -c "select count(*) from yt_videos where upload_date_source is distinct from 'exact'"; }
before=$(pending)
echo "Offen: $before"
if [ "$before" -eq 0 ]; then echo "ALLE EXAKT"; exit 0; fi
docker exec "$DB" psql -U postgres -d postgres -At -c "select video_id from yt_videos where upload_date_source is distinct from 'exact' order by video_id" > "$OUT/ids.txt"
split -l "$BATCH" "$OUT/ids.txt" "$OUT/chunk_"
: > "$OUT/all.tsv"; errors=0
for f in "$OUT"/chunk_*; do
  ids=$(paste -sd, "$f")
  resp=$(curl -s -m 30 "https://www.googleapis.com/youtube/v3/videos?part=snippet&id=${ids}&key=${KEY}")
  if echo "$resp" | jq -e '.error' >/dev/null 2>&1; then
    echo "$resp" | jq -r '.error.message' | head -1 >&2
    errors=$((errors+1)); [ "$errors" -ge 3 ] && { echo "Abbruch nach 3 API-Fehlern." >&2; break; }
    continue
  fi
  echo "$resp" | jq -r '.items[] | .id + "|" + (.snippet.publishedAt[0:10] | gsub("-";""))' >> "$OUT/all.tsv"
done
echo "Erhalten: $(wc -l < "$OUT/all.tsv")"
if [ -s "$OUT/all.tsv" ]; then
  docker cp "$OUT/all.tsv" "$DB:/tmp/yt_exact.tsv"
  docker exec -i "$DB" psql -U postgres -d postgres <<SQL
CREATE TEMP TABLE tmp_exact(video_id text PRIMARY KEY, upload_date text);
COPY tmp_exact(video_id, upload_date) FROM '/tmp/yt_exact.tsv' WITH (FORMAT text, DELIMITER '|');
UPDATE yt_videos v SET upload_date=to_date(t.upload_date,'YYYYMMDD'), upload_date_source='exact', upload_date_precision='day', metadata_synced_at=now()
FROM tmp_exact t WHERE v.video_id=t.video_id AND v.upload_date_source IS DISTINCT FROM 'exact';
SQL
fi
after=$(pending)
echo "Offen: $before -> $after"
