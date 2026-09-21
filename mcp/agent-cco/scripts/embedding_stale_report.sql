-- Read-only: Ist-Zustand der Embedding-Metadaten und Stale-Readiness.
-- Kanonisch seit der MXBAI-Migration (2026-09-18): embedding_version = 'mxbai-v1'
-- (Modell mxbai-embed-large, 1024 Dim) UND source_hash = sha256(exakt eingebetteter Text).
-- Fuer yt_chunk ist der eingebettete Text == gespeicherter content (fitForEmbedding ist dort
-- ein No-Op: Header + 800-Zeichen-Chunk liegt unter EMBED_MAX_CHARS=1500).
-- Aufruf: docker exec -i openbrain-db psql -U postgres -d postgres < embedding_stale_report.sql
\pset pager off
-- vector_dims()/Operator <=> liegen im Schema "extensions" -> ohne diesen search_path
-- schlagen die Dimensionspruefungen fehl ("function vector_dims(extensions.vector) does not exist").
SET search_path TO public, extensions;

SELECT '1) Verteilung nach Version' AS section;
SELECT artifact_type, coalesce(embedding_version, '(null)') AS version,
       count(*) AS rows
FROM agent_workspace
WHERE embedding IS NOT NULL
GROUP BY 1,2
ORDER BY 1,3 DESC;

SELECT '2) yt_chunk Hash-Konsistenz (sha256(content) == source_hash?)' AS section;
SELECT coalesce(embedding_version,'(null)') AS version,
       count(*) AS rows,
       count(*) FILTER (WHERE encode(sha256(convert_to(content,'UTF8')),'hex') = source_hash) AS hash_ok
FROM agent_workspace
WHERE artifact_type='yt_chunk' AND embedding IS NOT NULL
GROUP BY 1 ORDER BY 2 DESC;

SELECT '3) Stale-Kandidaten (Version != mxbai-v1)' AS section;
SELECT artifact_type, coalesce(embedding_version,'(null)') AS version, count(*) AS stale_rows
FROM agent_workspace
WHERE embedding IS NOT NULL AND coalesce(embedding_version,'') <> 'mxbai-v1'
GROUP BY 1,2 ORDER BY 3 DESC;

SELECT '4) Zeilen ohne Vektor (Duplikat-Check, muss 0 sein)' AS section;
SELECT artifact_type, status, count(*) AS rows_without_vector
FROM agent_workspace
WHERE embedding IS NULL
GROUP BY 1,2 ORDER BY 3 DESC;

SELECT '5) Vektor-Dimension (muss ueberall 1024 sein)' AS section;
SELECT artifact_type, vector_dims(embedding) AS dims, count(*)
FROM agent_workspace
WHERE embedding IS NOT NULL
GROUP BY 1,2 ORDER BY 3 DESC;

SELECT '6) Nebentabellen' AS section;
SELECT 'open_brain' AS tbl, count(*) AS rows, count(embedding) AS with_vector, min(vector_dims(embedding)) AS min_dim, max(vector_dims(embedding)) AS max_dim FROM open_brain
UNION ALL SELECT 'x_users', count(*), count(embedding), min(vector_dims(embedding)), max(vector_dims(embedding)) FROM x_users
UNION ALL SELECT 'yt_channels', count(*), count(embedding), min(vector_dims(embedding)), max(vector_dims(embedding)) FROM yt_channels;
