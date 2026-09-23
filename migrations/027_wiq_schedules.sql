-- ============================================================================
-- 027_wiq_schedules.sql
--
-- Klasse B: eine STEHENDE AUFGABE. Ein change_item traegt eine Zeitregel und
-- wird zum faelligen Zeitpunkt materialisiert: pro Vorkommen entsteht eine
-- Instanz (eigenes change_item) mit EINEM Workitem, das den Prompt mit den
-- MCP-Werkzeugen der Rolle ausfuehrt, die in der Vorlage steht.
--
-- Modell (bewusst am bestehenden Schema, keine neue Tabelle):
--
--   Definition  = change_items Zeile mit is_template = true, state = 'scheduled',
--                 schedule = Regel, next_run_at = naechster Termin.
--                 Dazu GENAU EIN Workitem vom Typ 'template' — die Vorlage, die
--                 Rolle, payload, Budget und max_attempts traegt. Sie wird NIE
--                 selbst dispatched (ready_workitems schliesst sie aus).
--
--   Instanz     = change_items Zeile mit state = 'running' und
--                 source_change_item_id -> Definition. Sie enthaelt die Kopie des
--                 Vorlagen-Workitems als Typ 'task'. step_key ueberlebt die Kopie:
--                 damit sind die Vorkommen einer stehenden Aufgabe vergleichbar
--                 ("dieselbe Pruefung in Lauf 1 vs. Lauf 12").
--
-- WARUM next_run_at NULL + state='scheduled' "noch nicht scharf" heisst:
--   Die Terminberechnung (Zeitzone, DST, Kalender) lebt an GENAU EINER Stelle —
--   in services/secretary/secretary/schedules.py. Der MCP-Server schreibt die
--   Regel und keine Termine; die Sekretaerin scharft beim naechsten Tick
--   (next_run_at ist null -> Termin setzen). So gibt es keine zweite,
--   abweichende Terminlogik in TypeScript.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/027_wiq_schedules.sql
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Die Regel und ihr Zustand.
-- ---------------------------------------------------------------------------
alter table change_items
  add column if not exists schedule     jsonb not null default '{}'::jsonb,
  add column if not exists next_run_at  timestamptz,
  add column if not exists last_run_at  timestamptz,
  add column if not exists run_count    integer not null default 0;

comment on column change_items.schedule is
  'Zeitregel der stehenden Aufgabe: {kind: once|repeat, ...}. Leer = ad-hoc-Lauf. '
  'Wird nur von der Sekretaerin interpretiert (secretary/schedules.py).';
comment on column change_items.next_run_at is
  'Naechster Faelligkeitszeitpunkt (Instant, UTC). NULL bei state=scheduled = noch nicht scharf. '
  'NULL bei state=done = keine weiteren Vorkommen.';
comment on column change_items.last_run_at is
  'Wann dieses Vorkommen zuletzt materialisiert wurde.';
comment on column change_items.run_count is
  'Wie oft diese Definition schon materialisiert wurde (Vorkommen).';
comment on column change_items.is_template is
  'true = Definition einer stehenden Aufgabe. Wird nie selbst ausgefuehrt; '
  'materialisiert sich als Instanz mit source_change_item_id -> diese Zeile.';

alter table change_items drop constraint if exists change_items_state_check;
alter table change_items add  constraint change_items_state_check
  check (state in ('running','done','failed','silent','scheduled','cancelled'));

alter table change_items drop constraint if exists change_items_run_count_check;
alter table change_items add  constraint change_items_run_count_check check (run_count >= 0);

comment on column change_items.state is
  'running = Lauf (oder Instanz) in Arbeit | scheduled = Definition wartet auf ihren Termin | '
  'done/failed/silent/cancelled = terminal.';

-- ---------------------------------------------------------------------------
-- Indizes: faellige Definitionen und die Instanzen einer Definition.
-- ---------------------------------------------------------------------------
create index if not exists change_items_due_idx
  on change_items (next_run_at) where state = 'scheduled';
create index if not exists change_items_source_idx
  on change_items (source_change_item_id) where source_change_item_id is not null;
create index if not exists workitems_template_idx
  on workitems (change_item_id) where type = 'template';

-- ---------------------------------------------------------------------------
-- Der Vorlagen-Typ. Eigener Typ statt Statusmissbrauch: eine Vorlage ist kein
-- wartender Auftrag, und ready_workitems darf sie gar nicht erst anbieten.
-- ---------------------------------------------------------------------------
alter table workitems drop constraint if exists workitems_type_check;
alter table workitems add  constraint workitems_type_check
  check (type in ('initial','task','review','template'));

-- ---------------------------------------------------------------------------
-- Bereitschaft NEU AUFBAUEN. Postgres friert die Spaltenliste einer View bei
-- der Erstellung ein (siehe 020) — deshalb steht sie hier vollstaendig.
-- Neu ist genau eine Zeile: Vorlagen sind nie bereit.
-- ---------------------------------------------------------------------------
create or replace view ready_workitems as
select w.*
from workitems w
where w.status = 'pending'
  and w.type <> 'template'
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

comment on view ready_workitems is
  'Abgeleitete Bereitschaft (I2): pending, kein unerfuelltes depends_on-Gate, keine Vorlage.';

-- ---------------------------------------------------------------------------
-- Rechte. PostgREST greift als anon/service_role zu und hat ohne explizite
-- Grants keinen Zugriff auf neue Spalten/Sichten (42501).
-- ---------------------------------------------------------------------------
grant all    on change_items, workitems to anon, service_role;
grant select on ready_workitems to anon, service_role;

notify pgrst, 'reload schema';
