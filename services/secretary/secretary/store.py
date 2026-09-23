"""Datenschicht der Sekretaerin.

Jede Operation ist eine einzelne Anweisung (autocommit) oder laeuft in einer
expliziten Transaktion. Es gibt genau EINEN Schreiber fuer Statusuebergaenge
ausserhalb der Worker: diese Datei.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    """Autocommit-Verbindung. psycopg liest PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE."""
    with psycopg.connect(row_factory=dict_row, autocommit=True) as conn:
        yield conn


@contextmanager
def transaction() -> Iterator[psycopg.Cursor]:
    """Mehrere Anweisungen atomar (fuer submit und Genehmigungen)."""
    with psycopg.connect(row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit()


# ── Wartung ────────────────────────────────────────────────────────────────

def recover_leases() -> int:
    """Abgelaufene Leases zurueck auf pending. Gibt die Anzahl zurueck."""
    with connect() as conn:
        row = conn.execute("select workitem_recover_leases() as n").fetchone()
        return int(row["n"]) if row else 0


def skip_cascade() -> int:
    """Unerreichbare Tasks ueberspringen (Reviews ausgenommen), rekursiv."""
    with connect() as conn:
        row = conn.execute("select workitem_skip_cascade() as n").fetchone()
        return int(row["n"]) if row else 0


# ── Auswahl und Lease ──────────────────────────────────────────────────────

def ready_items(limit: int = 20) -> list[dict[str, Any]]:
    """Bereite Items, hoechste Prioritaet zuerst."""
    with connect() as conn:
        rows = conn.execute(
            """
            select w.id, w.change_item_id, w.step_key, w.type, w.role, w.round,
                   w.attempts, w.max_attempts, w.priority, w.payload, w.budget,
                   w.reasoning_effort
              from ready_workitems w
              join change_items ci on ci.id = w.change_item_id
             where ci.state = 'running'
             order by w.priority desc, w.created_at
             limit %s
            """,
            (limit,),
        ).fetchall()
        return list(rows)


def claim(item_id: str, owner: str, lease_seconds: int) -> dict[str, Any] | None:
    """Item in Lease nehmen. None, wenn es jemand anderes war (Vergleich-und-Setze)."""
    with connect() as conn:
        row = conn.execute(
            """
            update workitems
               set status = 'running',
                   lease_owner = %s,
                   lease_expires_at = now() + make_interval(secs => %s),
                   started_at = now(),
                   attempts = attempts + 1
             where id = %s and status = 'pending'
            returning id, step_key, type, role, round, attempts, max_attempts,
                      reasoning_effort
            """,
            (owner, lease_seconds, item_id),
        ).fetchone()
        return dict(row) if row else None


def requeue(item_id: str, reason: str) -> None:
    """Zurueck auf pending — fuer den Retry. Der Grund bleibt im Log, nicht in der DB."""
    with connect() as conn:
        conn.execute(
            """
            update workitems
               set status = 'pending', lease_owner = null, lease_expires_at = null
             where id = %s and status = 'running'
            """,
            (item_id,),
        )


def fail(item_id: str, reason: str) -> None:
    """Item endgueltig scheitern lassen (Protokollverletzung oder Retries erschoepft)."""
    with connect() as conn:
        conn.execute(
            """
            update workitems
               set status = 'failed', finished_at = now(),
                   lease_owner = null, lease_expires_at = null,
                   result = coalesce(result, '{}'::jsonb) || jsonb_build_object('secretary_reason', %s::text)
             where id = %s and status = 'running'
            """,
            (reason, item_id),
        )


def add_usage(item_id: str, rounds: int, tokens: int, seconds: int,
              cache_read: int = 0, output: int = 0) -> None:
    """Ist-Verbrauch aufaddieren. Mehrere Versuche summieren sich — die Frage
    des Reviews ist 'was hat dieses Item insgesamt gekostet', nicht 'was der
    letzte Versuch'."""
    with connect() as conn:
        conn.execute(
            """
            update workitems
               set usage = jsonb_build_object(
                     'rounds',          coalesce((usage->>'rounds')::int, 0) + %s,
                     'tokens',          coalesce((usage->>'tokens')::int, 0) + %s,
                     'seconds',         coalesce((usage->>'seconds')::int, 0) + %s,
                     'cacheReadTokens', coalesce((usage->>'cacheReadTokens')::int, 0) + %s,
                     'outputTokens',    coalesce((usage->>'outputTokens')::int, 0) + %s)
             where id = %s
            """,
            (rounds, tokens, seconds, cache_read, output, item_id),
        )


def load_item(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "select id, change_item_id, step_key, type, role, status, round, payload, "
            "       attempts, max_attempts, reasoning_effort "
            "  from workitems where id = %s",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None


def context_predecessors(item_id: str) -> list[dict[str, Any]]:
    """Die context-Kanten: wessen Ergebnisse dieses Item lesen soll."""
    with connect() as conn:
        rows = conn.execute(
            """
            select p.id, p.step_key, p.status
              from workitem_links l
              join workitems p on p.id = l.to_id
             where l.from_id = %s and l.kind = 'context'
             order by p.step_key
            """,
            (item_id,),
        ).fetchall()
        return list(rows)


# ── Rollen und Sessions ────────────────────────────────────────────────────

def role(name: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "select name, patch, model, max_concurrency, description, reasoning_effort "
            "  from roles where name = %s",
            (name,),
        ).fetchone()
        return dict(row) if row else None


def roles_catalog() -> list[dict[str, Any]]:
    """Alle Rollen mit Faehigkeiten — die Entscheidungsgrundlage des Leads.

    Die Beschreibung ist der einzige Ort, an dem der Lead erfaehrt, welche
    Datenquelle eine Rolle besitzt. Sie gehoert in die roles-Tabelle und nicht
    in den Code: eine neue Rolle ist damit ein Patch plus eine Zeile SQL.
    """
    with connect() as conn:
        rows = conn.execute(
            "select name, description, max_concurrency, reasoning_effort "
            "  from roles order by name"
        ).fetchall()
        return list(rows)


REASONING_LEVELS = ("off", "low", "high", "max")


def set_role_effort(role_name: str, effort: str | None) -> bool:
    """Reasoning-Level einer Rolle setzen. None = DSH-Default (settings.yaml).

    Laufende Rollen-Prozesse behalten ihren Level: er wird beim Start gesetzt.
    Die Aenderung wirkt fuer neue (Rolle, Level)-Kombinationen.
    """
    if effort is not None and effort not in REASONING_LEVELS:
        raise ValueError(
            f"unbekannter Reasoning-Level {effort!r}; erlaubt: {', '.join(REASONING_LEVELS)}")
    with connect() as conn:
        cur = conn.execute(
            "update roles set reasoning_effort = %s where name = %s", (effort, role_name))
        return cur.rowcount == 1


def session_for(change_item_id: str, role_name: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "select session_id from agent_sessions where change_item_id = %s and role = %s",
            (change_item_id, role_name),
        ).fetchone()
        return row["session_id"] if row else None


def ensure_session(change_item_id: str, role_name: str, candidate: str) -> str:
    """Die Session dieser Rolle fuer diesen Lauf — beim ersten Mal angelegt.

    Insert-dann-Lesen statt Lesen-dann-Schreiben: wenn zwei Items derselben Rolle
    gleichzeitig starten, gewinnt genau eine Session, und beide bekommen sie.
    """
    with connect() as conn:
        conn.execute(
            """
            insert into agent_sessions (change_item_id, role, session_id)
            values (%s, %s, %s)
            on conflict (change_item_id, role) do nothing
            """,
            (change_item_id, role_name, candidate),
        )
        row = conn.execute(
            "select session_id from agent_sessions where change_item_id = %s and role = %s",
            (change_item_id, role_name),
        ).fetchone()
        return str(row["session_id"])


# ── Abschluss ──────────────────────────────────────────────────────────────

def finalize_change_items() -> list[dict[str, Any]]:
    """Laeufe abschliessen, in denen nichts mehr offen ist. Gibt die Abschluesse zurueck.

    Der LETZTE abgeschlossene Review entscheidet — nicht "irgendwo steht ein failed".
    Sonst faerbt ein in Runde 1 gerissenes Budget, das Runde 2 geloest hat, den
    ganzen Lauf rot. Genau das ist im Live-Lauf passiert.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            update change_items ci
               set state = case
                     when (select (w.result->>'satisfied')::boolean
                             from workitems w
                            where w.change_item_id = ci.id
                              and w.type = 'review' and w.status = 'done'
                            order by w.round desc, w.created_at desc limit 1) is true
                       then 'done'
                     when (select (w.result->>'satisfied')::boolean
                             from workitems w
                            where w.change_item_id = ci.id
                              and w.type = 'review' and w.status = 'done'
                            order by w.round desc, w.created_at desc limit 1) is false
                       then 'failed'
                     when exists (select 1 from workitems w
                                   where w.change_item_id = ci.id and w.status = 'failed')
                       then 'failed'
                     else 'done'
                   end,
                   finished_at = now()
             where ci.state = 'running'
               and not exists (select 1 from workitems w
                                where w.change_item_id = ci.id
                                  and w.status in ('pending', 'running'))
            returning id, title, state
            """
        ).fetchall()
        return list(rows)


# ── Stehende Aufgaben (Klasse B) ───────────────────────────────────────────
# Eine Definition ist ein change_item mit is_template=true, state='scheduled'
# und einem Workitem vom Typ 'template'. Sie wird nie selbst ausgefuehrt; jedes
# Vorkommen materialisiert sich als Instanz (eigenes change_item) mit einer
# KOPIE der Vorlage. Die Terminlogik steht NICHT hier, sondern in schedules.py —
# diese Datei schreibt nur, was der Tick entschieden hat.

def unarmed_definitions(limit: int = 50) -> list[dict[str, Any]]:
    """Definitionen ohne Termin: frisch angelegt, warten auf ihr Scharfstellen."""
    with connect() as conn:
        rows = conn.execute(
            """
            select id, title, entry_prompt, schedule, created_at, run_count
              from change_items
             where state = 'scheduled' and is_template and next_run_at is null
             order by created_at
             limit %s
            """,
            (limit,),
        ).fetchall()
        return list(rows)


def due_definitions(limit: int = 50) -> list[dict[str, Any]]:
    """Definitionen, deren Termin erreicht ist. Aeltester Termin zuerst."""
    with connect() as conn:
        rows = conn.execute(
            """
            select id, title, entry_prompt, schedule, created_at, run_count, next_run_at,
                   max_rounds
              from change_items
             where state = 'scheduled' and next_run_at is not null and next_run_at <= now()
             order by next_run_at
             limit %s
            """,
            (limit,),
        ).fetchall()
        return list(rows)


def arm(definition_id: str, next_run_at: Any, expected_created_at: Any) -> bool:
    """Termin setzen. Vergleich-und-Setze auf (state='scheduled', next_run_at is null).

    Der zweite Vergleich auf created_at ist kein Zierrat: er bindet das
    Scharfstellen an genau die Zeile, die gelesen wurde.
    """
    with connect() as conn:
        cur = conn.execute(
            """
            update change_items
               set next_run_at = %s
             where id = %s and state = 'scheduled'
               and next_run_at is null and created_at = %s
            """,
            (next_run_at, definition_id, expected_created_at),
        )
        return cur.rowcount == 1


def stop_definition(definition_id: str, state: str, error: str | None = None) -> None:
    """Definition endgueltig beenden: kein weiterer Termin. state ist 'done',
    'cancelled' oder 'failed'; bei 'failed' wandert der Grund in die Regel,
    damit er im Dashboard sichtbar ist und nicht nur im Log steht."""
    with connect() as conn:
        conn.execute(
            """
            update change_items
               set state = %s,
                   next_run_at = null,
                   finished_at = now(),
                   schedule = case when %s::text is null then schedule
                                   else schedule || jsonb_build_object('error', %s::text) end
             where id = %s and state = 'scheduled'
            """,
            (state, error, error, definition_id),
        )


def materialize(
    definition: dict[str, Any],
    expected_next: Any,
    new_next: Any,
    new_state: str,
    context: dict[str, Any],
) -> str | None:
    """EIN Vorkommen materialisieren — alles in EINER Transaktion.

    1. Termin fortschreiben (Vergleich-und-Setze auf den gelesenen Wert).
    2. Instanz anlegen (state='running', source_change_item_id -> Definition).
    3. Die Vorlage als 'task' kopieren.

    Gibt die Instanz-id zurueck. None = jemand anderes hat den Termin gezogen
    (der Tick laeuft dann einfach weiter). Schlaegt Schritt 3 fehl, faellt die
    ganze Transaktion zurueck — eine Instanz ohne Workitem waere ein Lauf, den
    niemand abarbeiten kann (dieselbe Regel wie bei run_submit).
    """
    with transaction() as cur:
        cur.execute(
            """
            update change_items
               set next_run_at = %s, last_run_at = now(), run_count = run_count + 1,
                   state = %s
             where id = %s and state = 'scheduled' and next_run_at = %s
            returning run_count
            """,
            (new_next, new_state, definition["id"], expected_next),
        )
        advanced = cur.fetchone()
        if advanced is None:
            return None

        payload_context = dict(context)
        payload_context["occurrence"] = advanced["run_count"]

        cur.execute(
            """
            insert into change_items (title, entry_prompt, state, source_change_item_id,
                                      max_rounds)
            values (%s, %s, 'running', %s, coalesce(%s, 2))
            returning id
            """,
            (definition["title"], definition["entry_prompt"], definition["id"],
             definition.get("max_rounds")),
        )
        instance_id = cur.fetchone()["id"]

        cur.execute(
            """
            insert into workitems (change_item_id, step_key, type, role, payload,
                                   priority, max_attempts, budget)
            select %s, t.step_key, 'task', t.role,
                   t.payload || jsonb_build_object('schedule', %s::jsonb),
                   t.priority, t.max_attempts, t.budget
              from workitems t
             where t.change_item_id = %s and t.type = 'template'
             order by t.created_at
            """,
            (instance_id, psycopg.types.json.Jsonb(payload_context), definition["id"]),
        )
        if cur.rowcount == 0:
            raise RuntimeError(
                "Vorlage fehlt: change_item " + str(definition["id"])
                + " hat kein Workitem vom Typ 'template'"
            )
        return str(instance_id)


def definitions(limit: int = 200) -> list[dict[str, Any]]:
    """Alle stehenden Aufgaben mit ihrem Zustand — fuer CLI und Dashboard."""
    with connect() as conn:
        rows = conn.execute(
            """
            select d.id, d.title, d.state, d.schedule, d.next_run_at, d.last_run_at,
                   d.run_count,
                   (select count(*) from change_items i
                     where i.source_change_item_id = d.id) as vorkommen,
                   (select i.state from change_items i
                     where i.source_change_item_id = d.id
                     order by i.created_at desc limit 1) as letzte_instanz
              from change_items d
             where d.is_template
             order by d.created_at desc
             limit %s
            """,
            (limit,),
        ).fetchall()
        return list(rows)


def cancel_definition(definition_id: str) -> bool:
    """Stehende Aufgabe abschalten. Kein Statusuebergang an Workitems — die
    Definition bekommt einfach keinen Termin mehr."""
    with connect() as conn:
        cur = conn.execute(
            """
            update change_items
               set state = 'cancelled', next_run_at = null, finished_at = now()
             where id = %s and state = 'scheduled'
            """,
            (definition_id,),
        )
        return cur.rowcount == 1


def trigger_now(definition_id: str) -> bool:
    """Definition sofort faellig stellen. Materialisiert wird weiterhin nur von
    der Sekretaerin — hier wird ausschliesslich der Termin gezogen."""
    with connect() as conn:
        cur = conn.execute(
            """
            update change_items
               set next_run_at = now()
             where id = %s and state = 'scheduled' and next_run_at is not null
            """,
            (definition_id,),
        )
        return cur.rowcount == 1


def create_definition(
    title: str,
    prompt: str,
    role: str,
    schedule: dict[str, Any],
    payload: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    max_attempts: int = 2,
    priority: int = 0,
    max_rounds: int | None = None,
) -> str:
    """Definition + Vorlagen-Workitem anlegen. Eine Transaktion: eine Definition
    ohne Vorlage waere ein Termin, der ins Leere feuert."""
    with transaction() as cur:
        cur.execute(
            """
            insert into change_items (title, entry_prompt, state, is_template, schedule,
                                      max_rounds)
            values (%s, %s, 'scheduled', true, %s::jsonb, coalesce(%s, 2))
            returning id
            """,
            (title, prompt, psycopg.types.json.Jsonb(schedule), max_rounds),
        )
        definition_id = cur.fetchone()["id"]
        base_payload = dict(payload or {})
        base_payload.setdefault("prompt", prompt)
        cur.execute(
            """
            insert into workitems (change_item_id, step_key, type, role, payload,
                                   priority, max_attempts, budget)
            values (%s, 'aufgabe', 'template', %s, %s::jsonb, %s, %s, %s::jsonb)
            """,
            (definition_id, role, psycopg.types.json.Jsonb(base_payload),
             priority, max_attempts, psycopg.types.json.Jsonb(budget or {})),
        )
        return str(definition_id)


# ── Eingang ────────────────────────────────────────────────────────────────

def submit(prompt: str, title: str | None = None, budget: dict | None = None,
           max_rounds: int | None = None) -> tuple[str, str]:
    """Boss-Prompt -> change_item + initial-Workitem. Gibt (change_item_id, workitem_id).

    max_rounds ist der Rundendeckel des Laufs; None laesst den Spalten-Default (2)."""
    clean = prompt.strip()
    if not clean:
        raise ValueError("Der Prompt ist leer.")
    headline = (title or clean.splitlines()[0])[:120]
    with transaction() as cur:
        cur.execute(
            "insert into change_items (title, entry_prompt, max_rounds) "
            "values (%s, %s, coalesce(%s, 2)) returning id",
            (headline, clean, max_rounds),
        )
        change_item_id = cur.fetchone()["id"]
        cur.execute(
            """
            insert into workitems (change_item_id, step_key, type, role, priority, payload, budget)
            values (%s, 'initial', 'initial', 'lead_engineer', 100, %s::jsonb, %s::jsonb)
            returning id
            """,
            (change_item_id, psycopg.types.json.Jsonb({"prompt": clean}),
             psycopg.types.json.Jsonb(budget or {})),
        )
        workitem_id = cur.fetchone()["id"]
    return str(change_item_id), str(workitem_id)


# ── Diagnose ───────────────────────────────────────────────────────────────

def graph(change_item_id: str) -> list[dict[str, Any]]:
    """Der Lauf als Liste, fuer die Kommandozeile."""
    with connect() as conn:
        rows = conn.execute(
            """
            select step_key, type, role, status, round, attempts, budget, usage,
                   (select count(*) from workitem_links l where l.from_id = w.id) as kanten,
                   left(coalesce(result::text, ''), 80) as result_kurz
              from workitems w
             where change_item_id = %s
             order by created_at
            """,
            (change_item_id,),
        ).fetchall()
        return list(rows)


def change_item(change_item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "select id, title, state, entry_prompt, created_at, finished_at "
            "  from change_items where id = %s",
            (change_item_id,),
        ).fetchone()
        return dict(row) if row else None
