-- Phase 5b: published_at physisch entfernen (ADR-3), erst nach Abschluss des exact-Upgrades.
-- Voraussetzung: kein Code liest/schreibt yt_videos.published_at mehr.
-- Sicherung: yt_videos_backup_20260914_032843 enthaelt die Spalte weiterhin.
BEGIN;
ALTER TABLE yt_videos DROP COLUMN IF EXISTS published_at;
COMMIT;
