-- ============================================================================
-- 030_wiq_runs_fortschritt.sql
--
-- Die Lesesicht bekommt "running_since": seit wann arbeitet dieser Lauf?
--
-- WARUM: workitems.usage schreibt die Sekretaerin erst beim Einsammeln, also
-- NACH dem Ende eines Items. Waehrend ein Item laeuft, steht in der Datenbank
-- kein Verbrauch — im Dashboard sah ein 8-Minuten-Scan deshalb aus wie Stillstand.
-- running_since ist die ehrliche Fortschrittsanzeige, die es ohne Zutun der
-- Agenten gibt: der Startzeitpunkt des aeltesten laufenden Items.
--
-- WARUM DROP + CREATE statt CREATE OR REPLACE: die neue Spalte steht in der
-- Mitte (vor den Aggregaten), und ein REPLACE kann weder Spaltennamen noch
-- -reihenfolge aendern. BEGIN/COMMIT macht den Tausch atomar, damit in der
-- Zwischenzeit keine Abfrage ins Leere laeuft. Grants muessen neu gesetzt
-- werden — sie verschwinden mit der Sicht.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/030_wiq_runs_fortschritt.sql
-- ============================================================================

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
       ci.created_at,
       ci.finished_at,
       ci.source_change_item_id,
       -- Aeltestes laufendes Item: "laeuft seit ..." ohne Agenten-Mitarbeit.
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
  'Lesesicht des WIQ-Dashboards: eine Zeile pro Lauf mit Item-Zahl, Kosten-Roll-up '
  'und running_since (Fortschritt ohne Agenten-Mitarbeit). Nur lesen.';

grant select on wiq_runs to anon, service_role;

commit;

notify pgrst, 'reload schema';
