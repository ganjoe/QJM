-- ============================================================================
-- 028_wiq_dashboard_runs.sql
--
-- Lesesicht fuer das Dashboard: eine Zeile pro change_item, mit Item-Zahl,
-- offenen Items und Kosten-Roll-up.
--
-- WARUM ALS VIEW: change_items.usage fuellt niemand (die Sekretaerin schreibt
-- nur workitems.usage). Die Kosten eines Laufs entstehen erst durch die Summe
-- ueber seine Items. Diese Summe gehoert in die Datenbank — sonst muesste der
-- Client alle Workitems laden, nur um eine Zahl zu zeigen.
--
-- Der Zugriff laeuft ueber PostgREST (wie bei allen MCP-Servern). Deshalb:
-- Rechte fuer anon/service_role und ein Schema-Reload am Ende.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/028_wiq_dashboard_runs.sql
-- ============================================================================

create or replace view wiq_runs as
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
       -- Workitems ohne Vorlagen: eine Vorlage ist kein Arbeitsschritt.
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
  'Lesesicht des WIQ-Dashboards: eine Zeile pro Lauf mit Item-Zahl und Kosten-Roll-up. '
  'Nur lesen — Statusuebergaenge macht ausschliesslich die Sekretaerin.';

create index if not exists change_items_kind_idx on change_items (is_template, created_at desc);

grant select on wiq_runs to anon, service_role;

notify pgrst, 'reload schema';
