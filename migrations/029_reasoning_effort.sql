-- ============================================================================
-- 029_reasoning_effort.sql
--
-- Reasoning-Level pro Rolle und pro Workitem.
--
-- WARUM:
--   DSH loest den Level so auf:  requested ?? model.defaultEffort
--   Die Sekretaerin uebergab bisher NICHTS — also galt der Default aus
--   ~/.dsh/settings.yaml (agent-default-model.reasoningEffort: max) fuer JEDEN
--   Lauf, auch fuer ein triviales Item. 'max' ist der teuerste Wert.
--
--   Ab jetzt:  workitem.reasoning_effort  ->  role.reasoning_effort  ->  DSH-Default
--
-- WICHTIG (Betriebsgrenze):
--   Der Level wird bei der INITIALISIERUNG eines DSH-Prozesses gesetzt, nicht
--   pro Session. Ein Rollen-Prozess traegt also genau einen Level. Die
--   Sekretaerin haelt deshalb einen Prozess PRO (Rolle, Level) — die
--   Parallelitaet (roles.max_concurrency) gilt weiterhin pro Rolle.
--
-- WERTE: off | low | high | max   (llm-deepseek: REASONING_EFFORTS)
--   Ein nicht unterstuetzter Wert laesst den Modellaufruf mit
--   UNSUPPORTED_REASONING_EFFORT scheitern — deshalb die CHECK-Constraints.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/029_reasoning_effort.sql
-- ============================================================================

alter table roles
  add column if not exists reasoning_effort text;

alter table workitems
  add column if not exists reasoning_effort text;

alter table roles drop constraint if exists roles_reasoning_check;
alter table roles add  constraint roles_reasoning_check
  check (reasoning_effort is null or reasoning_effort in ('off','low','high','max'));

alter table workitems drop constraint if exists workitems_reasoning_check;
alter table workitems add  constraint workitems_reasoning_check
  check (reasoning_effort is null or reasoning_effort in ('off','low','high','max'));

comment on column roles.reasoning_effort is
  'Vorgabe der Rolle: off|low|high|max. NULL = DSH-Default aus settings.yaml. '
  'Wird beim Start des Rollen-Prozesses gesetzt (ein Prozess pro Rolle+Level).';
comment on column workitems.reasoning_effort is
  'Ueberschreibung des Lead Engineers fuer EIN Item (off|low|high|max). '
  'Schlaegt roles.reasoning_effort. NULL = Vorgabe der Rolle.';

-- Startwerte: der Lead braucht den Kopf, Arbeiter brauchen ihn seltener.
-- Bewusst EXPLIZIT statt NULL, damit der laufende Wert sichtbar ist.
update roles set reasoning_effort = 'high'
 where reasoning_effort is null;

notify pgrst, 'reload schema';
