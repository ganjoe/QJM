#!/usr/bin/env python3
"""Split the PCA master universe ticker list (from the MCP spill file) into
chunk files so that parallel scan agents can each process a slice."""
import json
import os
import re

SPILL = "/tmp/dsh-spill-gQcchN/session-6fc3237c90a0/56f2549d7a49-mcp__openbrain-pca__manage_watchlist.txt"
OUT_DIR = "/home/daniel/QJM/rs_scan_chunks"
CHUNK_SIZE = 200

with open(SPILL, "r", encoding="utf-8") as fh:
    raw = fh.read()

match = re.search(r'^JSON: (\{.*\})\s*$', raw, flags=re.MULTILINE)
if not match:
    raise SystemExit("JSON block not found in spill file")

payload = json.loads(match.group(1))
tickers = payload["tickers"]

# Drop virtual / non-equity entries
clean = [
    t.strip()
    for t in tickers
    if t.strip() and not t.strip().startswith("$") and not t.strip().startswith("_")
]
clean = sorted(set(clean))

os.makedirs(OUT_DIR, exist_ok=True)
for old in os.listdir(OUT_DIR):
    os.remove(os.path.join(OUT_DIR, old))

chunks = [clean[i:i + CHUNK_SIZE] for i in range(0, len(clean), CHUNK_SIZE)]
for idx, chunk in enumerate(chunks, start=1):
    path = os.path.join(OUT_DIR, f"chunk_{idx:02d}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(chunk) + "\n")

print(f"count_raw={len(tickers)} count_clean={len(clean)} chunks={len(chunks)} chunk_size={CHUNK_SIZE}")
print("--- chunk 01 ---")
print(",".join(chunks[0]))
print("--- chunk 02 ---")
print(",".join(chunks[1]))
print("--- last chunk ---")
print(",".join(chunks[-1]))
