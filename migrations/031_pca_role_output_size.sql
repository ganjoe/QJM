-- ============================================================================
-- 031_pca_role_output_size.sql
--
-- Die Rolle pca bekommt die Warnung, die der CCO laengst hat.
--
-- BEFUND (Lauf 47c7480b, 21.09.2026): zwei pca-Scans rissen ihr Budget
-- (627k und 761k Tokens bei 480k Soll, je 11 Runden, je ~8,5 Minuten) — bei nur
-- ~5 Werkzeugaufrufen. Der Agent hat die Ursache selbst benannt:
--
--   "teuer waren die sehr breiten Tabellen-Antworten (Universal-Scanner
--    liefert 250-Zeilen-Tabellen ~ 30k Tokens pro Lauf)"
--
-- Der Lead Engineer plant trotzdem kleine Budgets, weil er die Ausgabegroesse
-- der Rolle nicht kennt. Genau dafuer ist roles.description da: sie ist die
-- Entscheidungsgrundlage des Leads (framing.py schickt sie mit).
--
-- Anwenden:
--   docker exec -i openbrain-db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
--     < migrations/031_pca_role_output_size.sql
-- ============================================================================

update roles
   set description =
       'Charts und Technik: OHLCV-Historie, Indikatoren, technische Scanner, '
       'Marktbreite, Watchlists. Keine Fundamentaldaten, keine SEC-Filings, kein Broker-Konto. '
       'ACHTUNG AUSGABEGROESSE: Scanner liefern breite Tabellen (~30k Tokens pro Aufruf, '
       'bis 250 Zeilen). Immer mit KLEINEM limit scannen (60-80) und dafuer zweimal eng '
       'gefiltert, statt einmal breit — sonst reisst das Item-Budget bei wenigen Aufrufen. '
       'Namen/Stammdaten liegen bei cda, nicht hier.'
 where name = 'pca';

notify pgrst, 'reload schema';
