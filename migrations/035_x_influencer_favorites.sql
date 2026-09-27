-- 035: Influencer-Favoriten + OpenBrain-Beschreibung + Themen-Matching
--
-- favorite   = Geld-Gate: nur Favoriten laufen im regelmaessigen Sync
--              (taegliche Suche + Timeline-Abgleich). Neue Influencer starten mit false.
-- description= Link/Referenz auf den OpenBrain-Eintrag des Influencers
-- brain_ref  = UUID dieses open_brain-Eintrags (maschinenlesbar)
-- brain_content = lokale Kopie des Profiltextes ("gut bei den Themen ..."),
--              Basis fuer x_users.embedding und damit fuer die Themen-Suche.
--
-- Startbelegung: Favorit = alle aktiven Accounts mit WENIGER als 20 Posts/Tag
-- (Schnitt ueber die vollstaendig abgedeckten Tage der letzten 14 Tage).

alter table x_users add column if not exists favorite boolean not null default false;
alter table x_users add column if not exists description text;
alter table x_users add column if not exists brain_ref text;
alter table x_users add column if not exists brain_content text;

comment on column x_users.favorite is 'Nur Favoriten laufen im regelmaessigen Sync (Suche + Timeline-Abgleich). Neue Influencer: false.';
comment on column x_users.description is 'Link/Referenz auf den OpenBrain-Eintrag mit Beschreibung + Themenprofil.';
comment on column x_users.brain_ref is 'UUID des zugehoerigen open_brain-Eintrags.';
comment on column x_users.brain_content is 'Lokale Kopie des Beschreibungstextes (Basis fuer x_users.embedding).';

with days as (
  select (created_at at time zone 'UTC')::date as d, count(*) as c
  from agent_workspace
  where artifact_type = 'x_post' and created_at > now() - interval '14 days'
  group by 1
), usable as (
  select d from days where c > 1000 and d < (now() at time zone 'UTC')::date
), rate as (
  select lower(metadata->>'author') as author,
         count(*)::numeric / greatest((select count(*) from usable), 1) as per_day
  from agent_workspace
  where artifact_type = 'x_post'
    and (created_at at time zone 'UTC')::date in (select d from usable)
  group by 1
)
update x_users u
set favorite = (coalesce(r.per_day, 0) < 20)
from x_users u2
left join rate r on r.author = lower('@' || u2.username)
where u.username = u2.username and u2.is_active;

update x_users set favorite = false where not is_active;

-- Themen-Suche ueber Influencer-Profile (Tier 2: wen frage ich zu Thema X?)
create or replace function match_x_users(
  query_embedding extensions.vector,
  match_count integer default 5,
  p_favorites_only boolean default false
)
returns table (username text, screen_name text, favorite boolean, description text, brain_content text, similarity double precision)
language sql stable
set search_path to 'public', 'extensions'
as $$
  select u.username, u.screen_name, u.favorite, u.description, u.brain_content,
         1 - (u.embedding <=> query_embedding) as similarity
  from x_users u
  where u.is_active
    and u.embedding is not null
    and (not p_favorites_only or u.favorite)
  order by u.embedding <=> query_embedding
  limit greatest(1, match_count);
$$;

comment on function match_x_users is 'Naechstliegende Influencer-Profile zu einer Query-Embedding (Tier-2-Kandidatenauswahl).';

-- Kontrolle
select favorite, count(*) as accounts,
       round(min(coalesce((select 1),0))::numeric,0) as dummy
from x_users where is_active group by favorite order by favorite desc;
