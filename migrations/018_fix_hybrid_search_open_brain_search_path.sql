-- 018_fix_hybrid_search_open_brain_search_path.sql
--
-- BUGFIX (beim MXBAI-Cutover entdeckt): hybrid_search_open_brain war die EINZIGE
-- Vektor-RPC ohne "SET search_path TO 'public','extensions'". Der Operator <=> lebt im
-- Schema "extensions"; PostgREST laeuft mit PGRST_DB_SCHEMAS=public und
-- PGRST_DB_EXTRA_SEARCH_PATH=public, also OHNE extensions. Damit schlug jeder Aufruf mit
--   ERROR: operator does not exist: extensions.vector <=> extensions.vector
-- fehl -- das MCP-Tool search_thoughts war dauerhaft kaputt (auch schon vor der Migration).
--
-- Fix ohne Body-Umbau: search_path der Funktion setzen (wie bei allen anderen RPCs).

ALTER FUNCTION public.hybrid_search_open_brain(
  extensions.vector, text, double precision, integer, text
) SET search_path TO 'public', 'extensions';

NOTIFY pgrst, 'reload schema';
