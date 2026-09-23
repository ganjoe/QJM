-- ============================================================================
-- 032_change_item_max_rounds.sql
--
-- max_rounds wird eine Eigenschaft des Laufs statt eine Einstellung der
-- Sekretaerin (SECRETARY_MAX_ROUNDS galt bisher fuer ALLE Laeufe).
--
-- BEDEUTUNG: der Deckel fuer die Planungsrunden des Lead Engineers. Er steht
-- in der ersten Nachricht jedes Planungs- und Review-Items (framing.py) und
-- entscheidet, ob ein unbefriedigendes Review eine weitere Runde anstoesst
-- oder mit status=failed abschliesst. Er ist eine ANWEISUNG an den Lead, keine
-- harte Sperre — die Sekretaerin dispatcht weiterhin, was bereit ist.
--
-- DEFAULT 2: unveraendert gegenueber der bisherigen Einstellung.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/032_change_item_max_rounds.sql
-- ============================================================================

alter table change_items
  add column if not exists max_rounds integer not null default 2;

alter table change_items drop constraint if exists change_items_max_rounds_check;
alter table change_items add  constraint change_items_max_rounds_check
  check (max_rounds between 1 and 10);

comment on column change_items.max_rounds is
  'Deckel fuer die Planungsrunden des Lead Engineers (1..10). Wird jedem Item des '
  'Laufs als max_rounds gezeigt. Bei einer stehenden Aufgabe erbt jede Instanz den Wert.';

-- ---------------------------------------------------------------------------
-- Die Lesesicht muss die neue Spalte tragen. Sie steht in der Mitte, und ein
-- CREATE OR REPLACE kann die Spaltenreihenfolge nicht aendern — deshalb der
-- atomare Tausch (siehe 030).
-- ---------------------------------------------------------------------------
begin;

drop view if exists wiq_runs;

create view wiq_runs as
select ci.id,
       ci.title,
       ci.entry_prompt,
       ci.state,
       ci.is_template,
       ci.schedule,
       ci.next_run_at,
       ci.last_run_at,
       ci.run_count,
       ci.max_rounds,
       ci.created_at,
       ci.finished_at,
       ci.source_change_item_id,
       (select min(w.started_at) from workitems w
         where w.change_item_id = ci.id and w.status = 'running') as running_since,
       (select count(*) from workitems w
         where w.change_item_id = ci.id and w.type <> 'template') as items,
       (select count(*) from workitems w
         where w.change_item_id = ci.id and w.type <> 'template'
           and w.status in ('pending', 'running')) as offen,
       (select coalesce(sum(nullif(w.usage->>'tokens', '')::bigint), 0)
          from workitems w where w.change_item_id = ci.id) as tokens,
       (select coalesce(sum(nullif(w.usage->>'seconds', '')::bigint), 0)
          from workitems w where w.change_item_id = ci.id) as seconds
  from change_items ci;

comment on view wiq_runs is
  'Lesesicht des WIQ-Dashboards: eine Zeile pro Lauf mit Item-Zahl, Kosten-Roll-up, '
  'Fortschritt (running_since) und Rundendeckel. Nur lesen.';

grant select on wiq_runs to anon, service_role;

commit;

notify pgrst, 'reload schema';
