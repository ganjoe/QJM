-- ============================================================================
-- 033_ready_workitems_reasoning_effort.sql
--
-- Reparatur: ready_workitems kennt reasoning_effort nicht.
--
-- BEFUND:
--   029_reasoning_effort.sql hat die Spalte reasoning_effort zu workitems und
--   roles hinzugefuegt, aber die View ready_workitems NICHT neu erzeugt.
--   Postgres friert die Spaltenliste einer View bei der Erstellung ein
--   (siehe 020, ausdruecklich im Kopf von 027 vermerkt) — die View aus 027
--   endet daher bei usage.
--
--   services/secretary/secretary/store.py:58 selektiert aber
--   w.reasoning_effort aus ready_workitems:
--
--       select w.id, ..., w.budget, w.reasoning_effort
--         from ready_workitems w ...
--
--   Folge: jeder Tick der Sekretaerin bricht ab mit
--       psycopg.errors.UndefinedColumn: column w.reasoning_effort does not exist
--   und es wird kein Workitem mehr dispatcht.
--
-- FIX:
--   ready_workitems exakt wie in 027 neu aufbauen (select w.* expandiert die
--   Spaltenliste jetzt inklusive reasoning_effort), Grant erneuern, PostgREST
--   den Schema-Wechsel melden.
--
-- BETRIEBSGRENZE (Wiederholung aus 027, damit es nicht erneut passiert):
--   Jede kuenftige Aenderung an der Spaltenliste von workitems MUSS diese View
--   neu erzeugen. "select w.*" ist nur zum Erstellungszeitpunkt ein Stern.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/033_ready_workitems_reasoning_effort.sql
-- ============================================================================

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
  'Abgeleitete Bereitschaft (I2): pending, kein unerfuelltes depends_on-Gate, keine Vorlage. '
  'Enthaelt reasoning_effort (033) — bei jeder Spaltenaenderung an workitems neu erzeugen.';

-- PostgREST greift als anon/service_role zu und hat ohne explizite Grants
-- keinen Zugriff auf neue Spalten/Sichten (42501).
grant select on ready_workitems to anon, service_role;

notify pgrst, 'reload schema';
