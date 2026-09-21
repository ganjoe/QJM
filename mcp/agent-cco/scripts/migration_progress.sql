-- Fortschritt der MXBAI-Migration (read-only, beliebig oft ausfuehrbar).
\pset pager off
\timing off

SELECT '=== agent_workspace: Vektoren neu (1024) vs. alt (4096) ===' AS section;
SELECT artifact_type,
       count(*) AS rows,
       count(embedding) AS v_new_1024,
       count(embedding_old_4096) AS v_old_4096,
       count(*) FILTER (WHERE status = 'pending_embedding') AS pending_embedding
FROM agent_workspace
GROUP BY 1 ORDER BY 2 DESC;

SELECT '=== yt_chunks Detail ===' AS section;
SELECT count(*) AS chunks,
       count(embedding) AS with_new_vector,
       coalesce(sum(length(content)), 0) AS chars,
       round(avg(length(content))) AS avg_chars,
       max(length(content)) AS max_chars
FROM agent_workspace WHERE artifact_type = 'yt_chunk';

SELECT '=== X-Posts Detail ===' AS section;
SELECT count(*) AS posts, count(embedding) AS with_new_vector,
       round(avg(length(content))) AS avg_chars, max(length(content)) AS max_chars
FROM agent_workspace WHERE artifact_type = 'x_post';

SELECT '=== yt_videos Status ===' AS section;
SELECT status, count(*) FROM yt_videos GROUP BY 1 ORDER BY 2 DESC;

SELECT '=== Nebentabellen ===' AS section;
SELECT 'open_brain' t, count(*) rows, count(embedding) neu FROM open_brain
UNION ALL SELECT 'x_users', count(*), count(embedding) FROM x_users
UNION ALL SELECT 'yt_channels', count(*), count(embedding) FROM yt_channels;

SELECT '=== Dimensionen der neuen Vektoren (nur 1024 erlaubt) ===' AS section;
SELECT DISTINCT vector_dims(embedding) AS dims FROM agent_workspace WHERE embedding IS NOT NULL;

SELECT '=== Speicher ===' AS section;
SELECT pg_size_pretty(pg_total_relation_size('agent_workspace')) AS agent_workspace_total,
       pg_size_pretty(pg_total_relation_size('agent_workspace') - pg_indexes_size('agent_workspace')) AS heap_and_toast;

SELECT '=== Fehlerzustand (NULL-Vektor trotz status=embedded) ===' AS section;
SELECT artifact_type, count(*) FROM agent_workspace
WHERE status = 'embedded' AND embedding IS NULL GROUP BY 1;
