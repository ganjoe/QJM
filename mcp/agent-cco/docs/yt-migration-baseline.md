# Baseline vor Migration (Phase 0)

Stand: 2026-09-14, Tag 20260914_032843

## Sicherungen
- `yt_videos_backup_20260914_032843` — 3.887 Zeilen (64 MB)
- `aw_yt_backup_20260914_032843` — 95.365 yt_chunk-Zeilen, id+metadata+content+created_at (185 MB)
  (Embeddings wurden nicht kopiert; sie werden in Phase 3 auch nicht verändert.)

## Kennzahlen
- yt_videos: 3.887 | mit Datum: 111 | ohne: 3.776
- agent_workspace yt_chunk: 95.365 | mit metadata.published_at: 1.325
- DB/Timezone: Etc/UTC

## Golden Queries (müssen nach Phase 6 erfüllt sein)
1. "macro trading ideen von anfang 2022" -> nur upload_date in [2022-01-01, 2022-04-01), vollständige Provenienz.
2. "letzte videos von @traderlion" -> korrektes Datum, kein "Unbekannt", stabile Sortierung.
3. "was sagte traderlion 2021 über earnings gaps" -> Zeitfenster 2021, Timecode-Quelle nachvollziehbar.

## Baseline-Verhalten (vorher)
- `search_youtube_content("macro trading ideen anfang 2022")` lieferte open_brain-Notizen
  ("KISS FRAMEWORK...", "Test-Eintrag...", "Slingshot Pullback Pattern...") — KEINE Transkript-Chunks.
- `show_yt_content("@traderlion")` lieferte durchgehend "Datum: Unbekannt".
