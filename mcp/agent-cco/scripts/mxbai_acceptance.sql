-- P10 Abnahme der MXBAI-Migration (read-only).
-- Aufruf: docker exec -i openbrain-db psql -U postgres -d postgres < mxbai_acceptance.sql
\pset pager off
-- Wichtig: der Operator <=> und die Funktionen vector_dims() liegen im Schema "extensions".
-- Ohne diesen search_path schlagen die Plan-/Dimensionspruefungen mit
-- "operator does not exist: extensions.vector <=> extensions.vector" fehl.
SET search_path TO public, extensions;

SELECT '1) Schema: Vektor-Spalten (Soll: embedding=vector(1024), *_old_4096 nur bis zum Cleanup)' AS section;
SELECT c.relname AS tabelle, a.attname AS spalte, format_type(a.atttypid, a.atttypmod) AS typ
FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped
  AND format_type(a.atttypid, a.atttypmod) LIKE '%vector%'
ORDER BY 1, 2;

SELECT '2) yt_chunks-View: Typ + ACLs' AS section;
SELECT format_type(a.atttypid, a.atttypmod) AS view_spaltentyp
FROM pg_attribute a WHERE a.attrelid = 'public.yt_chunks'::regclass AND a.attname = 'embedding';
SELECT grantee, privilege_type FROM information_schema.role_table_grants
WHERE table_name = 'yt_chunks' AND privilege_type = 'SELECT' ORDER BY 1;

SELECT '3) HNSW-Indizes' AS section;
SELECT indexname, LEFT(indexdef, 120) AS def FROM pg_indexes
WHERE indexdef ILIKE '%hnsw%' ORDER BY 1;

SELECT '4) Daten: Version/Modell/Hash/Dimension' AS section;
SELECT artifact_type,
       count(*) AS rows,
       count(embedding) AS mit_vektor,
       count(*) FILTER (WHERE embedding IS NULL) AS ohne_vektor,
       count(*) FILTER (WHERE embedding_model = 'mxbai-embed-large') AS modell_ok,
       count(*) FILTER (WHERE embedding_version = 'mxbai-v1') AS version_ok,
       count(*) FILTER (WHERE source_hash IS NOT NULL) AS hash_gesetzt,
       min(vector_dims(embedding)) AS min_dim,
       max(vector_dims(embedding)) AS max_dim
FROM agent_workspace
GROUP BY 1 ORDER BY 2 DESC;

SELECT '5) yt_chunk: content == sha256(source_hash)?' AS section;
SELECT count(*) AS chunks,
       count(*) FILTER (WHERE encode(sha256(convert_to(content, 'UTF8')), 'hex') = source_hash) AS hash_konsistent
FROM agent_workspace WHERE artifact_type = 'yt_chunk' AND embedding IS NOT NULL;

SELECT '6) Nebentabellen' AS section;
SELECT 'open_brain' AS tbl, count(*) AS rows, count(embedding) AS mit_vektor,
       min(vector_dims(embedding)) AS min_dim, max(vector_dims(embedding)) AS max_dim FROM open_brain
UNION ALL SELECT 'x_users', count(*), count(embedding), min(vector_dims(embedding)), max(vector_dims(embedding)) FROM x_users
UNION ALL SELECT 'yt_channels', count(*), count(embedding), min(vector_dims(embedding)), max(vector_dims(embedding)) FROM yt_channels;

SELECT '7) Speicher (Ziel ~1,45 GiB Daten + Index)' AS section;
SELECT pg_size_pretty(pg_total_relation_size('agent_workspace')) AS agent_workspace_gesamt,
       pg_size_pretty(pg_relation_size('agent_workspace')) AS heap,
       pg_size_pretty(pg_indexes_size('agent_workspace')) AS index,
       pg_size_pretty(pg_total_relation_size('open_brain')) AS open_brain;

SELECT '8) Status-Verteilung (keine Huengenden)' AS section;
SELECT artifact_type, status, count(*) FROM agent_workspace GROUP BY 1, 2 ORDER BY 1, 3 DESC;
SELECT status, count(*) FROM yt_videos GROUP BY 1 ORDER BY 2 DESC;

SELECT '9) Plan: nutzt die Suche den HNSW-Index?' AS section;
SET enable_seqscan = off;
EXPLAIN (COSTS OFF)
SELECT id FROM agent_workspace
WHERE artifact_type = 'yt_chunk'
ORDER BY embedding <=> (SELECT embedding FROM agent_workspace WHERE embedding IS NOT NULL LIMIT 1)
LIMIT 10;
RESET enable_seqscan;
