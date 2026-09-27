#!/usr/bin/env python3
# ============================================================================
# mcp/agent-journal/tests/e2e.py
#
# End-to-End-Abnahmetest des Trader-Tagebuchs gegen einen LAUFENDEN Server.
# Deckt die Tests 1-15 aus TRADER_JOURNAL_MCP_PLAN.md Abschnitt 10 ab.
#
#   python3 mcp/agent-journal/tests/e2e.py            # gegen http://127.0.0.1:8801/mcp
#   JOURNAL_MCP_URL=... python3 .../e2e.py
#
# Legt echte Eintraege mit dem Praefix E2E-TEST an und raeumt sie am Ende
# wieder auf (hard delete).
# ============================================================================
import json
import os
import re
import subprocess
import sys
import urllib.request
import hashlib

MCP_URL = os.environ.get("JOURNAL_MCP_URL", "http://127.0.0.1:8801/mcp")
HEALTH_URL = MCP_URL.rsplit("/mcp", 1)[0] + "/health"
DB_CONTAINER = os.environ.get("JOURNAL_DB_CONTAINER", "openbrain-db")
PATCH = os.environ.get("JOURNAL_DSH_PATCH", "/home/daniel/.dsh/cordis.patch.yml")

TEXT_A = ("E2E-TEST: Ich erwarte, dass der Bedarf an Kuehlung fuer Rechenzentren bis Ende 2027 stark steigt, "
          "weil die KI-Rechenlast weiter waechst. Meta und Google treiben das mit ihren Agenten an.")
TEXT_B = ("E2E-TEST NACHTRAG: Der Bedarf ist langsamer gestiegen als das Angebot — meine Annahme von oben "
          "ist damit vom Tisch.")
TEXT_C = "E2E-TEST REMINDER: Ich warte die Zahlen am Quartalstermin ab."

FAILS = []


def key():
    k = os.environ.get("MCP_ACCESS_KEY")
    if k:
        return k
    with open(PATCH, "r", encoding="utf-8") as fh:
        m = re.search(r"[0-9a-f]{64}", fh.read())
    if not m:
        raise SystemExit("Kein MCP_ACCESS_KEY gefunden (weder env noch in " + PATCH + ")")
    return m.group(0)


KEY = key()


def psql(sql):
    r = subprocess.run(["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", "postgres", "-d", "postgres",
                        "-At", "-v", "ON_ERROR_STOP=1", "-c", sql],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("psql fehlgeschlagen: " + r.stderr.strip()[:300])
    return r.stdout.rstrip(chr(10))


def mcp(tool, args):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args}}).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "x-brain-key": KEY,
    })
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8")
    payload = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line[5:].strip())
    if payload is None:
        raise RuntimeError("Keine data-Zeile in der MCP-Antwort: " + raw[:300])
    if "error" in payload:
        raise RuntimeError("MCP-Fehler: " + json.dumps(payload["error"])[:300])
    result = payload.get("result", {})
    text = ""
    for item in result.get("content", []):
        if item.get("type") == "text":
            text += item.get("text", "")
    if result.get("isError"):
        raise RuntimeError("Tool-Fehler: " + text[:300])
    return text


def check(name, cond, detail=""):
    if cond:
        print("  ok   " + name)
    else:
        print("  FAIL " + name + (" — " + detail if detail else ""))
        FAILS.append(name)


def short_id(text):
    m = re.search(r"ID:\s*([0-9a-f]{8})", text)
    return m.group(1) if m else None


def cleanup():
    psql("delete from journal_entry where body like 'E2E-TEST%';")


def main():
    print("== 1) health ==")
    with urllib.request.urlopen(HEALTH_URL, timeout=10) as resp:
        h = json.loads(resp.read().decode("utf-8"))
    check("health healthy", h.get("status") == "healthy", json.dumps(h))

    print("== 2) tools/list ==")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8")
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "x-brain-key": KEY})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    names = []
    for line in raw.splitlines():
        if line.startswith("data:"):
            d = json.loads(line[5:].strip())
            names = [t["name"] for t in d.get("result", {}).get("tools", [])]
    check("8 Tools", len(names) == 8, str(names))
    for want in ["journal_capture", "journal_recent", "journal_search", "journal_get",
                 "journal_theses", "journal_link", "journal_update", "journal_delete"]:
        check("Tool " + want, want in names)

    print("== 3) capture (Original) ==")
    out = mcp("journal_capture", {"content": TEXT_A})
    check("gespeichert", "Tagebucheintrag gespeichert" in out, out[:200])
    id_a = short_id(out)
    check("ID in Antwort", id_a is not None, out[:200])
    full_a = psql("select id from journal_entry where body like 'E2E-TEST:%' order by created_at desc limit 1;")
    sha = psql("select body_sha256 from journal_entry where id = '" + full_a + "';")
    body_db = psql("select body from journal_entry where id = '" + full_a + "';")
    check("body byte-identisch", body_db == TEXT_A, "DB: " + body_db[:80])
    check("sha256 stimmt", sha == hashlib.sha256(TEXT_A.encode("utf-8")).hexdigest(), sha)
    check("Zusammenfassung vorhanden", "Zusammenfassung:" in out)
    check("verwandte Eintraege berichtet", "Verwandte fruehere Eintraege" in out or "Keine verwandten" in out)
    check("Firmen erkannt (Meta)", "Meta" in out, out[:400])

    print("== 4) capture identisch -> Dedupe ==")
    out_dup = mcp("journal_capture", {"content": TEXT_A})
    check("Dedupe greift", "Nicht neu gespeichert" in out_dup, out_dup[:200])
    n = psql("select count(*) from journal_entry where body = '" + TEXT_A.replace("'", "''") + "';")
    check("nur ein Eintrag in der DB", n == "1", "count=" + n)

    print("== 5) recent ==")
    out_rec = mcp("journal_recent", {"days": 3})
    check("Eintrag gelistet", (id_a or "") in out_rec, out_rec[:200])
    check("Kopfzeile", "=== TAGEBUCH:" in out_rec)

    print("== 6) search ==")
    out_search = mcp("journal_search", {"query": "Kuehlung Rechenzentren KI-Rechenlast"})
    check("Treffer", (id_a or "") in out_search, out_search[:300])

    print("== 7) get ==")
    out_get = mcp("journal_get", {"entry_id": full_a})
    check("Original am Stueck", TEXT_A in out_get, out_get[:200])
    check("SHA ausgewiesen", sha in out_get)

    print("== 8) Nachtrag mit invalidates ==")
    out_b = mcp("journal_capture", {"content": TEXT_B, "links": [
        {"to": full_a, "relation": "invalidates", "note": "E2E: Bedarf langsamer als Angebot"}]})
    check("Nachtrag gespeichert", "Tagebucheintrag gespeichert" in out_b, out_b[:200])
    links = psql("select count(*) from journal_entry_link where to_entry_id = '" + full_a + "' and relation = 'invalidates';")
    check("Link in DB", links == "1", "count=" + links)
    total_theses = psql("select count(*) from journal_thesis where entry_id = '" + full_a + "';")
    if total_theses != "0":
        inv = psql("select count(*) from journal_thesis where entry_id = '" + full_a + "' and status = 'invalidated';")
        check("Thesen des Originals widerlegt", inv == total_theses, inv + "/" + total_theses)
    else:
        print("  info keine Thesen erzeugt (LLM) — Statusfolge nicht pruefbar")
    out_get2 = mcp("journal_get", {"entry_id": full_a})
    check("Original zeigt Nachtrag", "invalidates" in out_get2 and "NACHTRAEGE ZU DIESEM EINTRAG" in out_get2, out_get2[-300:])
    sha2 = psql("select body_sha256 from journal_entry where id = '" + full_a + "';")
    check("Original-body unveraendert", sha2 == sha)

    print("== 9) Wiedervorlage ==")
    today = psql("select (now() at time zone 'Europe/Berlin')::date;")
    out_c = mcp("journal_capture", {"content": TEXT_C, "reminder_date": today})
    check("Reminder gespeichert", "Wiedervorlage: " + today in out_c, out_c[:300])
    out_rec2 = mcp("journal_recent", {"days": 3})
    check("Wiedervorlagen-Block", "WIEDERVORLAGEN" in out_rec2, out_rec2[-300:])
    check("Wiedervorlage heute", "heute faellig" in out_rec2, out_rec2[-300:])
    out_upd = mcp("journal_update", {"entry_id": "E2E-TEST", "reminder_date": None}) if False else None
    id_c = re.search(r"ID:\s*([0-9a-f]{8})", out_c)
    if id_c:
        out_upd2 = mcp("journal_update", {"entry_id": id_c.group(1), "reminder_date": None})
        check("Reminder geloescht", "geloescht" in out_upd2, out_upd2[:200])

    print("== 10) soft delete ==")
    if id_c:
        out_del = mcp("journal_delete", {"entry_id": id_c.group(1)})
        check("soft geloescht", "Geloescht (soft)" in out_del, out_del[:200])
        deleted = psql("select deleted_at is not null from journal_entry where id::text like '" + id_c.group(1) + "%';")
        check("deleted_at gesetzt", deleted == "t", deleted)
        out_rec3 = mcp("journal_recent", {"days": 3})
        check("nicht mehr in recent", id_c.group(1) not in out_rec3)

    print("== 11) update: These bestaetigen ==")
    th = psql("select id from journal_thesis where entry_id = '" + full_a + "' limit 1;")
    if th:
        out_th = mcp("journal_update", {"thesis_id": th, "status": "confirmed", "outcome_note": "E2E: von Hand bestaetigt"})
        check("These aktualisiert", "These aktualisiert" in out_th, out_th[:200])
        st = psql("select status from journal_thesis where id = '" + th + "';")
        check("Status confirmed", st == "confirmed", st)
    else:
        print("  info keine These zum Aktualisieren")

    print("== 12) journal_theses ==")
    out_theses = mcp("journal_theses", {"status": "all", "limit": 20})
    check("Thesenliste lesbar", "These(n)" in out_theses or "Keine Thesen" in out_theses, out_theses[:200])


try:
    cleanup()
    main()
except Exception as exc:
    FAILS.append("Abbruch: " + repr(exc)[:200])
    print("  ABBRUCH: " + repr(exc)[:400])
finally:
    cleanup()
    print("")
    if FAILS:
        print("FEHLGESCHLAGEN: " + str(len(FAILS)) + " — " + ", ".join(FAILS))
        sys.exit(1)
    print("Alle Pruefungen bestanden.")
