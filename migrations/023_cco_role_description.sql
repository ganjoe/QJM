-- ============================================================================
-- 023_cco_role_description.sql
--
-- Die Rollenbeschreibung ist die Entscheidungsgrundlage des Leads (framing.py
-- schickt sie in der ersten Nachricht mit). Sie muss die neue Faehigkeit nennen,
-- seit migrations/022_ticker_breadth.sql existiert — sonst waehlt der Lead fuer
-- Ranglisten weiter den teuren Weg ueber show_x_content.
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/023_cco_role_description.sql
-- ============================================================================

update roles
   set description = 'Social-Sentiment: X-Posts, YouTube-Transkripte, gespeicherte Influencer-Posts, '
                     'dazu Web-Scraping und OCR fuer Belege. Kann Ticker serverseitig aggregieren '
                     '(ticker_breadth: Breite im Zeitfenster + Neuzugaenge, dann ticker_evidence fuer Belege) '
                     '— fuer Ranglisten IMMER diesen Weg nehmen, nie den Korpus durchlesen. '
                     'Keine Kurse, Charts, Fundamentaldaten oder Broker-Daten.'
 where name = 'cco';

notify pgrst, 'reload schema';
