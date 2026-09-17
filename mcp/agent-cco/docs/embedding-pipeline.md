# Embedding-Pipeline: CPU-only Backend, keep_alive und Priorisierung

Stand: 2026-09-16

## Kurzfassung

Es gibt **genau ein** Embedding-Backend: den CPU-Container `llm-gw-ollama-cpu` mit dem Modell
`qwen3-embedding:8b` (4096 Dimensionen). Der Gateway `llm-gateway/switchyard/gateway.py` nimmt
alle Embedding-Anfragen entgegen, hält das Modell dauerhaft geladen und bedient die Anfragen
nach Prioritätsklassen: **x_search > x_post > yt**.

## Warum kein GPU-Backend

Der Container `llm-gw-ollama-gpu` wurde entfernt (Code und Compose) und gestoppt. Grund: die
AMD Radeon 780M hat 2 GB VRAM, das Modell belegt ~6,6 GB. Ollama lud es deshalb auf die CPU
(`PROCESSOR 100% CPU` in `ollama ps`) — ein zweites Backend brachte also keinen Rechenvorteil,
sondern nur einen zweiten Satz Modellzustand, der hätte warmgehalten werden müssen.

## Warum kein Modell-Fallback

`qwen3-embedding:8b` liefert 4096 Dimensionen, und der Index ist auf `vector(4096)` ausgelegt.
Ein Fallback auf ein anderes Modell würde Vektoren eines fremden Raums in denselben Index
schreiben — der Schaden wäre still und dauerhaft (Ähnlichkeitssuche liefert dann sinnlose
Treffer, ohne Fehlermeldung). Der Gateway fällt deshalb **nie** auf ein anderes Modell aus.
Ist das Backend nicht erreichbar, antwortet er mit HTTP 503 (`backend_unreachable`).

## keep_alive: Modell wird niemals entladen

Zwei Ebenen, beide notwendig:

| Ebene | Ort | Wirkung |
|---|---|---|
| `OLLAMA_KEEP_ALIVE=-1` | `llm-gateway/docker-compose.yml` (Service `ollama-cpu`) | gilt für **alle** Requests, auch für Aufrufer, die das Feld nicht setzen |
| `keep_alive: -1` im Request | `gateway.py`, Env `EMBED_KEEP_ALIVE` | explizit pro Anfrage |

Wichtig: Der **OpenAI-kompatible** Endpoint `/v1/embeddings` von Ollama **ignoriert** das Feld
`keep_alive` (verifiziert: Modell zeigte danach weiter `4 minutes from now`). Der Gateway geht
deshalb auf die **native** API `/api/embed`, die das Feld respektiert (`Forever`).

Verifiziert: Nach 6,5 Minuten Leerlauf zeigte `ollama ps` weiterhin `Forever`.

## Priorisierung

Ollama serialisiert Requests pro Modell-Instanz und kennt keine Prioritäten. Damit eine
interaktive Suche nicht hinter großen Batch-Jobs wartet, laufen alle Anfragen durch **einen**
Scheduler-Task im Gateway. Die Klasse kommt als Body-Feld `priority` mit:

| Klasse | Rang | Verwendung |
|---|---|---|
| `x_search` | 30 | interaktive Suche: X-Suche, Open-Brain-Suche, YouTube-Suche |
| `x_post` | 20 | X-Posts vektorisieren, Profil- und Dokument-Embeddings |
| `yt` | 10 | YouTube-Transkript-Chunks (Massenlast) |

Fehlt oder unbekannt → wird wie `x_search` behandelt (Default `EMBED_DEFAULT_PRIORITY_CLASS`),
damit ein nicht angepasster Aufrufer nie in der langsamsten Klasse landet. Unbekannte Werte
werden geloggt.

Der Scheduler bündelt bis zu `EMBED_BATCH_SIZE` (8) Inputs pro Backend-Call. Größere Anfragen
werden in Chunks zerlegt; jeder Chunk hat ein eigenes Future, und das Future des Aufrufers wird
erst erfüllt, wenn **alle** Chunks zurück sind — in Summe genau so viele Vektoren wie Inputs,
in der ursprünglichen Reihenfolge.

Messung (2000-Item-Job der Klasse `yt` läuft, Query startet 1,2 s später):

```
Query als x_search: 0,95 s
Query als yt:     290,00 s   (gleiche Warteschlange, keine Bevorzugung)
=> Priorisierung: 306x
```

## OLLAMA_NUM_PARALLEL

Per Benchmark bestimmt. Getestet mit 1, 2 und 4 Slots:

| Slots | Durchsatz | Einzel-Latenz (warm) | Query-Latenz unter Batch-Last |
|---|---|---|---|
| 1 | ~6,3 Inputs/s | 0,123 s | 0,217 s |
| 2 | ~6,3 Inputs/s | 0,126 s | 0,212 s |
| 4 | ~6,3 Inputs/s | 0,124 s | 0,225 s |

Kein messbarer Unterschied — die CPU ist der Flaschenhals, nicht die Slot-Anzahl. Deshalb
`OLLAMA_NUM_PARALLEL=1`: maximaler Kontext pro Anfrage (4096 Tokens ungeteilt). Höhere Werte
teilen den Kontext und bringen nichts.

Die Zahlen schwanken mit der Maschinenlast (nur ~1 Input/s statt ~6,3 Inputs/s wenn andere
Prozesse die CPU beanspruchen). Die Rangfolge der Klassen ist davon unabhängig.

## Betrieb

Status und Queue-Tiefe:

```bash
curl -s http://localhost:4000/embeddings/status | python3 -m json.tool
```

Wichtige Env-Variablen des Gateways:

| Variable | Default | Bedeutung |
|---|---|---|
| `EMBED_BATCH_SIZE` | 8 | Inputs pro Backend-Call |
| `EMBED_KEEP_ALIVE` | -1 | nie entladen |
| `EMBED_QUEUE_TIMEOUT` | 600 | Sekunden, nach denen eine Anfrage als nicht bedient gilt |
| `EMBED_MAX_RETRIES` | 2 | Wiederholungen bei Backend-Fehler |
| `EMBED_BASE_URL` | `http://host.docker.internal:11434/v1` | Fallback, wenn `routes.toml` nichts auflöst |

## Bekannte Kostenstelle: lange Inputs

Die Laufzeit skaliert überproportional mit der Input-Länge (ein Slot, CPU):

| Input | Dauer |
|---|---|
| ~50 Zeichen | 0,14 s |
| 8000 Zeichen (~2000 Tokens) | ~13,6 s |
| 20000 Zeichen | ~261 s |

Die YT-Chunks liegen bei ~8000 Zeichen (`chunkTranscript`), kosten also ~13 s pro Chunk. Das
ist der dominierende Posten der YT-Pipeline und der Grund, warum die YT-Klasse zuletzt bedient
wird. Eine Verkleinerung der Chunks würde die Gesamtlaufzeit senken, weil die Aufmerksamkeit
quadratisch mit der Sequenzlänge wächst — das ist eine offene Optimierung, keine Anforderung.
