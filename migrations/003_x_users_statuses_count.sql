-- 003_x_users_statuses_count.sql
-- Kostenoptimierung TwitterAPI.io:
-- Billiger Liveness-Check ("hat der Influencer seit dem letzten Sync gepostet?")
-- über user/batch_info_by_ids (18 Credits/User) statt für jeden Influencer
-- eine Timeline-Page zu ziehen (≈300 Credits/User pro Zyklus).

alter table if exists public.x_users
  add column if not exists statuses_count integer;

comment on column public.x_users.statuses_count is
  'Zuletzt gesehener statusesCount (Gesamtzahl Status-Updates) für den günstigen Liveness-Check vor Timeline-Fetches. NULL = Baseline fehlt, wird beim nächsten Zyklus gesetzt.';

-- PostgREST Schema-Cache neu laden, damit die Spalte sofort per REST nutzbar ist
notify pgrst, 'reload schema';
