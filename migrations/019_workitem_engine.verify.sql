-- ============================================================================
-- 019_workitem_engine.verify.sql  —  Verifikation der Datenschicht
--
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/019_workitem_engine.verify.sql
--
-- Beide Szenarien laufen in einer Transaktion, die zurueckgerollt wird.
-- Es bleibt kein Testdatum in der Datenbank.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- Szenario A: Erfolgspfad  INITIAL -> (T1, T2|T1, T3) -> REVIEW|T1,T2,T3
-- ---------------------------------------------------------------------------
begin;

insert into change_items (id, title, entry_prompt) values
  ('aaaaaaaa-0000-4000-8000-000000000001',
   'TEST A: Sentiment SPX Rally',
   'Wie ist das Sentiment auf X zur aktuellen SPX-Rally?');

insert into workitems (id, change_item_id, step_key, type, role) values
  ('bbbbbbbb-0000-4000-8000-000000000001','aaaaaaaa-0000-4000-8000-000000000001','initial','initial','lead_engineer');

\echo ''
\echo '--- A1) nach Anlage: erwartet genau [initial] ---'
select step_key, type, status from ready_workitems
 where change_item_id = 'aaaaaaaa-0000-4000-8000-000000000001' order by step_key;

update workitems set status='done', finished_at=now(), result='{"tasks":4}'::jsonb
 where id = 'bbbbbbbb-0000-4000-8000-000000000001';

insert into workitems (id, change_item_id, step_key, type, role, parent_id) values
  ('bbbbbbbb-0000-4000-8000-000000000011','aaaaaaaa-0000-4000-8000-000000000001','t1','task','cco','bbbbbbbb-0000-4000-8000-000000000001'),
  ('bbbbbbbb-0000-4000-8000-000000000012','aaaaaaaa-0000-4000-8000-000000000001','t2','task','cco','bbbbbbbb-0000-4000-8000-000000000001'),
  ('bbbbbbbb-0000-4000-8000-000000000013','aaaaaaaa-0000-4000-8000-000000000001','t3','task','cco','bbbbbbbb-0000-4000-8000-000000000001'),
  ('bbbbbbbb-0000-4000-8000-000000000014','aaaaaaaa-0000-4000-8000-000000000001','t4','review','lead_engineer','bbbbbbbb-0000-4000-8000-000000000001');

insert into workitem_links (from_id, to_id, kind) values
  ('bbbbbbbb-0000-4000-8000-000000000012','bbbbbbbb-0000-4000-8000-000000000011','depends_on'),
  ('bbbbbbbb-0000-4000-8000-000000000014','bbbbbbbb-0000-4000-8000-000000000011','depends_on'),
  ('bbbbbbbb-0000-4000-8000-000000000014','bbbbbbbb-0000-4000-8000-000000000012','depends_on'),
  ('bbbbbbbb-0000-4000-8000-000000000014','bbbbbbbb-0000-4000-8000-000000000013','depends_on');

\echo '--- A2) erwartet [t1, t3] (t2 wartet auf t1, t4 auf alle) ---'
select step_key, type, status from ready_workitems
 where change_item_id = 'aaaaaaaa-0000-4000-8000-000000000001' order by step_key;

update workitems set status='done', finished_at=now() where id='bbbbbbbb-0000-4000-8000-000000000011';
\echo '--- A3) t1 fertig: erwartet [t2, t3] ---'
select step_key from ready_workitems where change_item_id='aaaaaaaa-0000-4000-8000-000000000001' order by step_key;

update workitems set status='done', finished_at=now() where id in
  ('bbbbbbbb-0000-4000-8000-000000000012','bbbbbbbb-0000-4000-8000-000000000013');
\echo '--- A4) alle Tasks fertig: erwartet [t4] ---'
select step_key, type from ready_workitems where change_item_id='aaaaaaaa-0000-4000-8000-000000000001' order by step_key;

rollback;

-- ---------------------------------------------------------------------------
-- Szenario B: Fehlerpfad  t1 FAILED -> t2 wird skipped, REVIEW laeuft trotzdem
-- ---------------------------------------------------------------------------
begin;

insert into change_items (id, title, entry_prompt) values
  ('aaaaaaaa-0000-4000-8000-000000000002','TEST B: Fehlerpfad','egal');

insert into workitems (id, change_item_id, step_key, type, role, status) values
  ('bbbbbbbb-0000-4000-8000-000000000021','aaaaaaaa-0000-4000-8000-000000000002','t1','task','cco','pending'),
  ('bbbbbbbb-0000-4000-8000-000000000022','aaaaaaaa-0000-4000-8000-000000000002','t2','task','cco','pending'),
  ('bbbbbbbb-0000-4000-8000-000000000023','aaaaaaaa-0000-4000-8000-000000000002','t3','task','cco','pending'),
  ('bbbbbbbb-0000-4000-8000-000000000024','aaaaaaaa-0000-4000-8000-000000000002','t4','review','lead_engineer','pending');

insert into workitem_links (from_id, to_id, kind) values
  ('bbbbbbbb-0000-4000-8000-000000000022','bbbbbbbb-0000-4000-8000-000000000021','depends_on'),
  ('bbbbbbbb-0000-4000-8000-000000000024','bbbbbbbb-0000-4000-8000-000000000021','depends_on'),
  ('bbbbbbbb-0000-4000-8000-000000000024','bbbbbbbb-0000-4000-8000-000000000022','depends_on'),
  ('bbbbbbbb-0000-4000-8000-000000000024','bbbbbbbb-0000-4000-8000-000000000023','depends_on');

update workitems set status='failed', finished_at=now() where id='bbbbbbbb-0000-4000-8000-000000000021';
update workitems set status='done',   finished_at=now() where id='bbbbbbbb-0000-4000-8000-000000000023';

\echo ''
\echo '--- B1) Skip-Kaskade: erwartet 1 (t2) ---'
select workitem_skip_cascade() as kaskadiert;

\echo '--- B2) Status: t1 failed, t2 skipped, t3 done, t4 review pending ---'
select step_key, type, status from workitems
 where change_item_id='aaaaaaaa-0000-4000-8000-000000000002' order by step_key;

\echo '--- B3) ready: erwartet [t4] — Review laeuft trotz Luecke ---'
select step_key, type from ready_workitems
 where change_item_id='aaaaaaaa-0000-4000-8000-000000000002' order by step_key;

\echo '--- B4) Lease-Erholung: erwartet 1 ---'
insert into workitems (id, change_item_id, step_key, type, role, status, lease_owner, lease_expires_at)
values ('bbbbbbbb-0000-4000-8000-000000000025','aaaaaaaa-0000-4000-8000-000000000002','t5','task','cco','running','worker-1', now() - interval '1 minute');
select workitem_recover_leases() as leases_erholt;

rollback;

\echo ''
\echo '--- C) Bleibende Objekte ---'
select table_name from information_schema.tables
 where table_schema='public' and table_name in ('change_items','workitems','workitem_links','roles','agent_sessions')
 order by table_name;
select name, patch, max_concurrency from roles order by name;
