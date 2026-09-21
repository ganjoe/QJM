-- ============================================================================
-- 021_roles_all_agents.sql
--
-- Zwei Dinge in einer Migration:
--   1. ALLE Agenten-Rollen als Workitem-Rollen verfuegbar machen. Bis hierher
--      gab es nur cco und lead_engineer — der Lead konnte nicht planen, was er
--      nicht kennt, und jede Aufgabe ausserhalb von Social-Sentiment war
--      unloesbar.
--   2. Dem Lead eine ENTSCHEIDUNGSGRUNDLAGE geben: die Spalte `description`.
--      framing.py sendet sie in der ersten Nachricht jedes Planungs- und
--      Review-Items mit. Vorher stand dort nur "available_roles: cco,
--      lead_engineer" — Namen ohne Faehigkeiten.
--
-- Die Beschreibung ist bewusst eine Zeile pro Rolle (die Rahmung zeigt sie
-- als Liste) und nennt immer auch, was die Rolle NICHT kann. Genau daran
-- scheitert der Lead sonst: er verteilt eine Aufgabe an eine Rolle, die die
-- Datenquelle gar nicht besitzt.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/021_roles_all_agents.sql
-- ============================================================================

alter table roles add column if not exists description text not null default '';

comment on column roles.description is
  'Faehigkeiten der Rolle in einem Satz, inklusive Abgrenzung. Wird dem Lead als '
  'Entscheidungsgrundlage in der ersten Nachricht gezeigt (framing.py).';

-- Rollen-Patches: roles/<name>.cordis.yml. model bleibt bewusst NULL, damit
-- ein manuell gesetztes Modell beim erneuten Anwenden nicht ueberschrieben wird.
insert into roles (name, patch, max_concurrency, description) values
  ('lead_engineer', 'roles/lead.cordis.yml', 1,
   'Plant Arbeit und bewertet Ergebnisse. Hat KEINEN Datenzugriff (keine Kurse, Charts, Social-Daten, kein Web) und verteilt die Arbeit als Workitems.'),
  ('cco', 'roles/cco.cordis.yml', 2,
   'Social-Sentiment: X-Posts, YouTube-Transkripte, gespeicherte Influencer-Posts, dazu Web-Scraping und OCR fuer Belege. Keine Kurse, Charts, Fundamentaldaten oder Broker-Daten.'),
  ('pta', 'roles/pta.cordis.yml', 1,
   'Broker-Sicht (IBKR): Live-Kurs, offene Positionen, Orders, Trade-Historie, Portfolio-Kennzahlen; kann Orders ausfuehren. Keine Charts, Fundamentaldaten oder Social-Daten.'),
  ('pca', 'roles/pca.cordis.yml', 2,
   'Charts und Technik: OHLCV-Historie, Indikatoren, technische Scanner, Marktbreite, Watchlists. Keine Fundamentaldaten, keine SEC-Filings, kein Broker-Konto.'),
  ('cda', 'roles/cda.cordis.yml', 1,
   'Daten und Dokumente: Chart-Download-Status, Ticker-Stammdaten (Shares, EPS, Umsatz), SEC-Filings und deren Destillate. Keine Live-Kurse, keine Chart-Analyse, keine Social-Daten.'),
  ('drawio', 'roles/drawio.cordis.yml', 1,
   'Erzeugt draw.io-Diagramme als Datei im dsh_playground und liest sie zurueck. Kein Zugriff auf Markt-, Social- oder Dokumentdaten.')
on conflict (name) do update
  set patch           = excluded.patch,
      max_concurrency = excluded.max_concurrency,
      description     = excluded.description;

-- grants: die neue Spalte erbt die Tabellen-Grants aus 019 (grant all on roles
-- an anon, service_role). PostgREST cacht das Schema -> reload.
notify pgrst, 'reload schema';
