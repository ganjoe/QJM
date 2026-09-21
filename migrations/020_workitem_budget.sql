-- ============================================================================
-- 020_workitem_budget.sql
--
-- Budgets pro Workitem. Der Lead schaetzt sie beim Planen, die DSH-seitige
-- Budget-Policy erzwingt sie, die Sekretaerin rechnet den Ist-Verbrauch nach.
--
-- Warum das noetig ist: DSH hat KEINE Rundengrenze (agent-loop/README.md:200
-- "No built-in turn budget"). Ohne Budget kann ein Item 151 Runden drehen —
-- gemessen, 36 Minuten fuer eine Frage, die 28 Sekunden braucht.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/020_workitem_budget.sql
--
-- BUDGET (Soll, vom Lead geschaetzt):
--   {"rounds": 12, "tokens": 400000, "seconds": 900}
--   Alle Felder optional. Fehlt das Objekt, gelten die Defaults der Policy.
--
-- USAGE (Ist, von der Sekretaerin aus dem Session-Log geschrieben):
--   {"rounds": 6, "tokens": 100227, "seconds": 28, "cacheReadTokens": 77696}
--
-- Metrik-Rangfolge: rounds ist die ehrlichste (der Lead kennt seine
-- Item-Granularitaet), tokens die Kostenmetrik, seconds nur Notausstieg —
-- sie misst Warteschlange und Provider-Latenz mit.
-- ============================================================================

alter table workitems
  add column if not exists budget jsonb not null default '{}'::jsonb,
  add column if not exists usage  jsonb not null default '{}'::jsonb;

alter table change_items
  add column if not exists budget jsonb not null default '{}'::jsonb,
  add column if not exists usage  jsonb not null default '{}'::jsonb;

comment on column workitems.budget is
  'Vom Lead geschaetztes Budget: {rounds, tokens, seconds}. Leer = Policy-Defaults.';
comment on column workitems.usage is
  'Ist-Verbrauch aus dem DSH-Session-Log: {rounds, tokens, seconds, cacheReadTokens}.';

-- Rechte wie bei den uebrigen neuen Objekten (PostgREST greift als service_role/anon).
grant all on workitems, change_items to anon, service_role;

notify pgrst, 'reload schema';
-- ---------------------------------------------------------------------------
-- Die View NEU AUFBAUEN. Postgres friert die Spaltenliste einer View bei der
-- Erstellung ein: 019 hat ready_workitems mit "select w.*" gebaut, die neuen
-- Spalten aus dieser Migration sind darin NICHT sichtbar. Ohne diese Zeile
-- scheitert jede Query mit "column w.budget does not exist".
-- ---------------------------------------------------------------------------
create or replace view ready_workitems as
select w.*
from workitems w
where w.status = 'pending'
  and not exists (
    select 1
    from workitem_links l
    join workitems p on p.id = l.to_id
    where l.from_id = w.id
      and l.kind = 'depends_on'
      and p.status <> all (
        case
          when w.type = 'review'
            then array['done','failed','skipped','cancelled']
          else array['done','skipped']
        end
      )
  );

grant select on ready_workitems to anon, service_role;

notify pgrst, 'reload schema';
