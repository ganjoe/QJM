-- ============================================================================
-- 019_workitem_engine.sql
--
-- Kernprinzip: ein persistenter Workitem-Graph. Eine LLM-freie Sekretaerin
-- arbeitet ihn ab, DSH fuehrt pro Workitem eine Agenten-Session aus.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/019_workitem_engine.sql
--
-- NICHT GEBAUT, ABER NICHT VERBAUT:
--   Periodische Tasks. Die Template-Felder in change_items sind vorhanden und
--   werden von nichts gelesen. step_key ueberlebt jede Kopie, damit spaeter
--   Laeufe vergleichbar sind ("check aus Lauf 1 vs. Lauf 2").
--
-- INVARIANTEN  (sie tragen das ganze System)
--   I1  Statusuebergaenge ueberlappen nie:
--         Sekretaerin : pending -> running (Lease) | running -> pending (Ablauf)
--         Worker      : running -> done | failed
--   I2  "blockiert" ist KEIN Status, sondern abgeleitet (siehe ready_workitems).
--   I3  skipped verhaelt sich wie done. Ein Vorgaenger mit failed/skipped/
--       cancelled macht abhaengige TASKS unerreichbar -> sie werden selbst
--       skipped. Ein REVIEW ist davon ausgenommen: er wartet auf Terminalitaet
--       statt auf Erfolg, damit er die Luecke bewerten kann statt zu haengen.
--   I4  parent_id ist die Baumachse (Herkunft), workitem_links sind die
--       Querkanten (Warte- und Lese-Beziehungen). Ein DAG ist kein Baum.
--   I5  workitem_links.from braucht workitem_links.to. depends_on = Gate
--       (Erfolg noetig), context = Lese-Kante (Ergebnis wird gezogen).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Der Lauf. Ein ad-hoc-Lauf entsteht aus einem Boss-Prompt, ein instanziierter
-- Lauf aus einem promoteten Change-Item (source_change_item_id + Version).
-- ---------------------------------------------------------------------------
create table if not exists change_items (
  id                    uuid primary key default gen_random_uuid(),
  title                 text not null,
  entry_prompt          text,                                   -- ad-hoc: der Boss-Prompt
  state                 text not null default 'running',
  -- Template-Felder: bewusst vorhanden, ungenutzt.
  is_template           boolean not null default false,
  template_version      integer,
  promoted_at           timestamptz,
  source_change_item_id uuid references change_items(id),
  created_at            timestamptz not null default now(),
  finished_at           timestamptz,
  constraint change_items_state_check check (state in ('running','done','failed','silent'))
);

-- ---------------------------------------------------------------------------
-- Ein Workitem = eine Zeile. step_key ist die stabile Knotenidentitaet
-- innerhalb eines Laufs und einer Runde.
-- ---------------------------------------------------------------------------
create table if not exists workitems (
  id               uuid primary key default gen_random_uuid(),
  change_item_id   uuid not null references change_items(id) on delete cascade,
  step_key         text not null,
  type             text not null,                               -- initial | task | review
  role             text not null,
  parent_id        uuid references workitems(id),               -- Baumachse, NICHT die Blockade
  payload          jsonb not null default '{}'::jsonb,          -- der Arbeitsauftrag (Prompt + Parameter)
  status           text not null default 'pending',
  result           jsonb,                                       -- schreibt der Worker selbst
  attempts         integer not null default 0,
  max_attempts     integer not null default 2,
  priority         integer not null default 0,
  round            integer not null default 1,
  lease_owner      text,
  lease_expires_at timestamptz,
  created_at       timestamptz not null default now(),
  started_at       timestamptz,
  finished_at      timestamptz,
  constraint workitems_type_check   check (type in ('initial','task','review')),
  constraint workitems_status_check check (status in ('pending','running','done','failed','skipped','cancelled')),
  constraint workitems_counter_check check (attempts >= 0 and max_attempts >= 1 and round >= 1),
  -- I1: laufend heisst "hat einen Lease", und nichts anderes.
  constraint workitems_lease_check check (
    (status = 'running') = (lease_owner is not null and lease_expires_at is not null)
  ),
  -- Terminal heisst "hat finished_at", und nichts anderes.
  constraint workitems_finished_check check (
    (status in ('done','failed','skipped','cancelled')) = (finished_at is not null)
  )
);

create unique index if not exists workitems_step_idx
  on workitems (change_item_id, step_key, round);

-- ---------------------------------------------------------------------------
-- Kanten. from braucht to.
-- ---------------------------------------------------------------------------
create table if not exists workitem_links (
  from_id    uuid not null references workitems(id) on delete cascade,
  to_id      uuid not null references workitems(id) on delete cascade,
  kind       text not null,
  created_at timestamptz not null default now(),
  primary key (from_id, to_id, kind),
  constraint workitem_links_kind_check check (kind in ('depends_on','context')),
  constraint workitem_links_no_self   check (from_id <> to_id)
);

-- ---------------------------------------------------------------------------
-- Rollen -> DSH-Anbindung. patch zeigt auf eine versionierte Datei im Repo
-- (roles/*.cordis.yml), nicht auf ein Profil in $DSH_HOME.
-- ---------------------------------------------------------------------------
create table if not exists roles (
  name            text primary key,
  patch           text not null,
  model           text,
  max_concurrency integer not null default 1,
  constraint roles_concurrency_check check (max_concurrency >= 1)
);

-- ---------------------------------------------------------------------------
-- Nur Rollen mit Gedaechtnis bekommen hier eine Zeile. Damit laufen INITIAL,
-- REVIEW Runde 1 und REVIEW Runde 2 in DERSELBEN DSH-Session.
-- ---------------------------------------------------------------------------
create table if not exists agent_sessions (
  change_item_id uuid not null references change_items(id) on delete cascade,
  role           text not null,
  session_id     text not null,
  created_at     timestamptz not null default now(),
  primary key (change_item_id, role)
);

-- ---------------------------------------------------------------------------
-- Indizes
-- ---------------------------------------------------------------------------
create index if not exists workitems_pending_idx
  on workitems (priority desc, created_at) where status = 'pending';
create index if not exists workitems_lease_idx
  on workitems (lease_expires_at) where status = 'running';
create index if not exists workitems_change_item_idx on workitems (change_item_id);
create index if not exists workitems_parent_idx      on workitems (parent_id);
create index if not exists workitem_links_from_idx   on workitem_links (from_id, kind);
create index if not exists workitem_links_to_idx     on workitem_links (to_id, kind);

-- ---------------------------------------------------------------------------
-- Bereitschaft ist ABGELEITET (I2 + I3).
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
            then array['done','failed','skipped','cancelled']   -- Review bewertet auch Luecken
          else array['done','skipped']                          -- Task laeuft nur auf Erfolg
        end
      )
  );

-- ---------------------------------------------------------------------------
-- Wartungsfunktionen der Sekretaerin. Beide sind bewusst in SQL: sie kodieren
-- I1 und I3, und in Python sind sie leicht falsch zu machen.
-- ---------------------------------------------------------------------------

-- Abgelaufene Leases zurueck auf pending (Crash-Erholung).
create or replace function workitem_recover_leases()
returns integer
language plpgsql
as $$
declare
  affected integer;
begin
  update workitems
     set status = 'pending', lease_owner = null, lease_expires_at = null
   where status = 'running'
     and lease_expires_at < now();
  get diagnostics affected = row_count;
  return affected;
end;
$$;

-- I3: abhaengige TASKS unerreichbarer Herkunft werden skipped, rekursiv.
-- Reviews bleiben unangetastet.
create or replace function workitem_skip_cascade()
returns integer
language plpgsql
as $$
declare
  affected integer;
  total integer := 0;
begin
  loop
    update workitems w
       set status = 'skipped', finished_at = now()
     where w.status = 'pending'
       and w.type <> 'review'
       -- ein Vorgaenger ist gescheitert/uebersprungen/abgebrochen ...
       and exists (
         select 1 from workitem_links l
           join workitems p on p.id = l.to_id
          where l.from_id = w.id and l.kind = 'depends_on'
            and p.status in ('failed','skipped','cancelled')
       )
       -- ... und alle Vorgaenger sind terminal (sonst wird nur gewartet).
       and not exists (
         select 1 from workitem_links l
           join workitems p on p.id = l.to_id
          where l.from_id = w.id and l.kind = 'depends_on'
            and p.status not in ('done','failed','skipped','cancelled')
       );
    get diagnostics affected = row_count;
    total := total + affected;
    exit when affected = 0;
  end loop;
  return total;
end;
$$;

-- ---------------------------------------------------------------------------
-- Rechte. PostgREST greift als service_role/anon zu und hat ohne explizite
-- Grants KEINEN Zugriff auf neue Tabellen (Fehler 42501). Konvention dieses
-- Repos: arwdDxtm fuer anon und service_role (siehe \dp pca_watchlists).
--
-- SICHERHEITSHINWEIS: damit darf jeder mit dem anon-Key den Workitem-Graphen
-- lesen UND schreiben. Das entspricht der bestehenden Konvention, ist aber die
-- Stelle, an der spaeter RLS nachgezogen werden sollte.
-- ---------------------------------------------------------------------------
grant all on change_items, workitems, workitem_links, roles, agent_sessions
  to anon, service_role;
grant select on ready_workitems to anon, service_role;
grant execute on function workitem_recover_leases(), workitem_skip_cascade()
  to anon, service_role;

-- ---------------------------------------------------------------------------
-- Startdaten: die beiden v1-Rollen. patch-Dateien entstehen in Phase 3.
-- ---------------------------------------------------------------------------
insert into roles (name, patch, max_concurrency) values
  ('lead_engineer', 'roles/lead.cordis.yml', 1),
  ('cco',           'roles/cco.cordis.yml',  2)
on conflict (name) do nothing;
