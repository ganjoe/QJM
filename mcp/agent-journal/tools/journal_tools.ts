// ============================================================================
// mcp/agent-journal/tools/journal_tools.ts
//
// Die 8 Tools des Trader-Tagebuchs. Aufbau und Beschreibungstexte folgen
// TRADER_JOURNAL_MCP_PLAN.md Abschnitt 6 (Zweck -> WHEN TO USE -> WHEN NOT TO USE).
//
// Grundsaetze:
//   * Der Originaltext wird nie umgeschrieben; Nachtraege sind neue Eintraege
//     mit einer Verknuepfung (journal_entry_link).
//   * Der Eintrag wird VOR dem LLM-Aufruf gespeichert: ein API-Fehler darf nie
//     einen Tagebucheintrag kosten.
//   * Alle Ausgaben sind kompakt und zeigen die UUID-Kurzform; ID-Parameter
//     akzeptieren volle UUID oder eindeutigen Praefix (Aufloesung in der DB).
// ============================================================================
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import {
  AGENT_ID,
  JOURNAL_DEFAULT_DAYS,
  JOURNAL_DEDUPE_WINDOW_HOURS,
  JOURNAL_MIN_RELATED_CHARS,
  JOURNAL_RELATED_LIMIT,
  JOURNAL_RELATED_THRESHOLD,
  JOURNAL_SEARCH_THRESHOLD,
  EMBED_MODEL_NAME,
  EMBED_VERSION,
  LlmUnavailableError,
  deepseekChatCompletion,
  extractJsonObject,
  fitForEmbedding,
  getDocumentEmbeddingDetailed,
  getQueryEmbedding,
  isIsoDate,
  joinList,
  loadJournalPrompt,
  log,
  oneLine,
  sha256Hex,
  supabase,
  todayInTz,
  uuidShort,
} from './llm.ts';

const NL = String.fromCharCode(10);

// --- kleine Helfer ---------------------------------------------------------

function ok(text: string) {
  return { content: [{ type: 'text' as const, text }] };
}

function err(text: string) {
  return { content: [{ type: 'text' as const, text }], isError: true };
}

function clampInt(v: unknown, def: number, min: number, max: number): number {
  const n = typeof v === 'number' && Number.isFinite(v) ? Math.trunc(v) : def;
  return Math.max(min, Math.min(max, n));
}

async function rpc(name: string, args: Record<string, unknown>): Promise<any> {
  const { data, error } = await supabase.rpc(name, args);
  if (error) throw new Error(name + ': ' + error.message);
  return data;
}

async function resolveId(prefixOrUuid: string, includeDeleted = false): Promise<string> {
  const data = await rpc('journal_resolve_id', { p_id: prefixOrUuid, p_include_deleted: includeDeleted });
  return String(data);
}

function strArray(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => String(x).trim()).filter((x) => x.length > 0);
}

function numOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : null;
}

function daysFromToday(iso: string | null): number | null {
  if (!iso) return null;
  const a = new Date(todayInTz() + 'T00:00:00Z').getTime();
  const b = new Date(iso.slice(0, 10) + 'T00:00:00Z').getTime();
  if (Number.isNaN(b)) return null;
  return Math.round((b - a) / 86400000);
}

function relDaysLabel(delta: number | null): string {
  if (delta === null) return '';
  if (delta < 0) return 'ueberfaellig seit ' + Math.abs(delta) + ' Tag(en)';
  if (delta === 0) return 'heute faellig';
  return 'in ' + delta + ' Tag(en)';
}

function fmtEntryLine(r: any): string {
  const parts: string[] = [];
  parts.push('[' + uuidShort(r.entry_id) + '] ' + (r.entry_date || '?') + ' · ' + (r.entry_type || 'observation'));
  if (r.reminder_date) parts.push('⏰ ' + r.reminder_date + ' (' + relDaysLabel(daysFromToday(r.reminder_date)) + ')');
  let line = parts.join(' | ');
  line += NL + '    ' + oneLine(r.summary || '(keine Zusammenfassung)', 260);
  const meta: string[] = [];
  if (r.tickers && r.tickers.length) meta.push('Ticker: ' + joinList(r.tickers));
  if (r.companies && r.companies.length) meta.push('Firmen: ' + joinList(r.companies));
  if (r.keywords && r.keywords.length) meta.push('Keywords: ' + joinList(r.keywords));
  if (meta.length) line += NL + '    ' + meta.join(' | ');
  const counts: string[] = [];
  if (typeof r.thesis_total === 'number') counts.push('Thesen: ' + r.thesis_open + '/' + r.thesis_total + ' offen');
  if (r.thesis_due) counts.push('davon ' + r.thesis_due + ' faellig');
  if (r.link_count) counts.push('Verknuepfungen: ' + r.link_count + ' (' + joinList(r.link_relations) + ')');
  if (counts.length) line += NL + '    ' + counts.join(' | ');
  return line;
}

function fmtThesisLine(r: any): string {
  let s = '[' + uuidShort(r.thesis_id) + '] ' + (r.status || 'open').toUpperCase() + ' · ' + (r.entry_date || '?');
  if (r.review_at) s += ' · Review ' + r.review_at + ' (' + relDaysLabel(daysFromToday(r.review_at)) + ')';
  s += NL + '    ' + oneLine(r.statement, 300);
  const meta: string[] = [];
  if (r.tickers && r.tickers.length) meta.push('Ticker: ' + joinList(r.tickers));
  if (r.companies && r.companies.length) meta.push('Firmen: ' + joinList(r.companies));
  if (r.horizon_days) meta.push('Horizont: ' + r.horizon_days + ' Tage');
  if (r.confidence) meta.push('Confidence: ' + r.confidence + '/5');
  meta.push('Eintrag: ' + uuidShort(r.entry_id));
  s += NL + '    ' + meta.join(' | ');
  if (r.falsifier) s += NL + '    Falsifikation: ' + oneLine(r.falsifier, 200);
  if (r.outcome_note) s += NL + '    Ergebnis: ' + oneLine(r.outcome_note, 200);
  return s;
}

function fmtRelated(r: any): string {
  let s = '- [' + uuidShort(r.entry_id) + '] ' + (r.entry_date || '?') + ' · ' + (r.entry_type || '');
  if (r.reminder_date) s += ' · ⏰ ' + r.reminder_date;
  s += NL + '  ' + oneLine(r.summary || '', 200);
  const meta: string[] = [];
  if (r.tickers && r.tickers.length) meta.push('Ticker: ' + joinList(r.tickers));
  if (r.companies && r.companies.length) meta.push('Firmen: ' + joinList(r.companies));
  if (meta.length) s += NL + '  ' + meta.join(' | ');
  if (typeof r.similarity === 'number') s += '  (Ähnlichkeit ' + r.similarity.toFixed(2) + ')';
  return s;
}

// --- LLM-Destillat ---------------------------------------------------------

type ThesisDraft = {
  statement: string;
  tickers: string[];
  companies: string[];
  horizon_days: number | null;
  falsifier: string | null;
  confidence: number | null;
};

type Distillate = {
  summary: string | null;
  keywords: string[];
  tickers: string[];
  companies: string[];
  themes: string[];
  entry_type: string | null;
  reminder_date: string | null;
  theses: ThesisDraft[];
};

const ENTRY_TYPES = ['thesis', 'observation', 'trade_note', 'review', 'lesson', 'macro'];

function normalizeDistillate(parsed: any): Distillate {
  const theses: ThesisDraft[] = [];
  if (Array.isArray(parsed.theses)) {
    for (const t of parsed.theses) {
      const statement = String(t?.statement || '').trim();
      if (!statement) continue;
      theses.push({
        statement,
        tickers: strArray(t?.tickers),
        companies: strArray(t?.companies),
        horizon_days: numOrNull(t?.horizon_days),
        falsifier: t?.falsifier ? String(t.falsifier).trim() : null,
        confidence: numOrNull(t?.confidence),
      });
    }
  }
  const et = String(parsed.entry_type || '').trim().toLowerCase();
  return {
    summary: parsed.summary ? String(parsed.summary).trim() : null,
    keywords: strArray(parsed.keywords),
    tickers: strArray(parsed.tickers),
    companies: strArray(parsed.companies),
    themes: strArray(parsed.themes),
    entry_type: ENTRY_TYPES.includes(et) ? et : null,
    reminder_date: isIsoDate(parsed.reminder_date) ? String(parsed.reminder_date) : null,
    theses,
  };
}

export async function distill(body: string, explicitReminder: string | null): Promise<{ d: Distillate | null; error: string | null }> {
  const system = loadJournalPrompt();
  let user = body;
  if (explicitReminder) {
    user += NL + NL + 'Hinweis: Der Nutzer hat als Wiedervorlagedatum ausdruecklich ' + explicitReminder +
      ' genannt. Uebernimm dieses Datum unveraendert als reminder_date.';
  }
  try {
    const content = await deepseekChatCompletion(
      [{ role: 'system', content: system }, { role: 'user', content: user }],
      { temperature: 0.1, maxTokens: 2500 },
    );
    const parsed = extractJsonObject(content);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { d: null, error: 'LLM-Antwort war kein JSON-Objekt' };
    }
    return { d: normalizeDistillate(parsed), error: null };
  } catch (e: any) {
    const retryable = !(e instanceof LlmUnavailableError) || e.retryable;
    return { d: null, error: String(e?.message || e) + (retryable ? '' : ' (nicht wiederholbar)') };
  }
}

export function embeddingText(d: Distillate | null, body: string): string {
  if (!d) return fitForEmbedding(body);
  const parts = [
    d.summary || '',
    d.keywords.length ? 'Keywords: ' + d.keywords.join(', ') : '',
    d.tickers.length ? 'Ticker: ' + d.tickers.join(', ') : '',
    d.companies.length ? 'Firmen: ' + d.companies.join(', ') : '',
    d.themes.length ? 'Themen: ' + d.themes.join(', ') : '',
  ].filter((p) => p.length > 0);
  return parts.length ? parts.join(NL) : fitForEmbedding(body);
}

// --- Registrierung ---------------------------------------------------------

export function registerJournalTools(server: McpServer) {
  // =========================================================================
  // 1) journal_capture
  // =========================================================================
  server.registerTool(
    'journal_capture',
    {
      title: 'Trader-Tagebuch — Eintrag schreiben',
      description:
        'Trader-Tagebuch: speichert eine EIGENE Marktmeinung, These, Beobachtung oder Lektion woertlich und am Stueck ' +
        '(der Originaltext ist das Tagebuch und wird nie umgeschrieben). Zusaetzlich erzeugt das System eine Zusammenfassung, ' +
        'Keywords, Ticker/Firmen, pruefbare Thesen mit Horizont und Falsifikationskriterium, ein Embedding und - falls der Text ' +
        'es hergibt - ein Wiedervorlagedatum.' + NL +
        'VERKNUEPFEN: Mit links [{to: "<uuid oder praefix>", relation: "follow_up|confirms|invalidates|related", note: "..."}] ' +
        'wird der neue Eintrag an einen bestehenden gehaengt. Bei relation invalidates werden die offenen Thesen des Originals ' +
        'als widerlegt markiert, bei confirms als bestaetigt - so wird Monate spaeter sichtbar, dass eine These erledigt ist, ' +
        'ohne den Originaltext anzufassen.' + NL +
        'Die Antwort enthaelt verwandte fruehere Eintraege MIT UUID - nutze sie fuer Vergleiche oder als Ziel einer Verknuepfung.' + NL +
        'NUR AUF AUSDRUECKLICHE ANWEISUNG — das ist die wichtigste Regel. Geschrieben wird ausschliesslich, wenn der Nutzer es ' +
        'unmissverstaendlich verlangt. Typische Ausloeser: "schreib das ins Tagebuch", "ins Journal", "ins Diary", "notier das", ' +
        '"journal das", "halt das fest", "trag das ein", "merk dir das". Nur dann journal_capture aufrufen.' + NL +
        'KEIN AUSLOESER: eine These, Meinung, Prognose oder Kausalitaetskette allein. "Ich glaube / ich erwarte / meiner Meinung ' +
        'nach" ist ein GESPRAECH, kein Schreibauftrag. Dann diskutierst du normal und bietest hoechstens an: "Soll ich das ins ' +
        'Tagebuch schreiben?" - und wartest auf ein klares Ja. Im Zweifel NICHT schreiben.' + NL +
        'Verknuepfen und Wiedervorlagen sind ebenfalls Schreibvorgaenge: links nur, wenn der Nutzer das Anhaengen ausdruecklich ' +
        'verlangt; reminder_date nur, wenn er einen Termin nennt UND das Eintragen verlangt.' + NL +
        'WHEN TO USE: ausschliesslich nach einem solchen ausdruecklichen Auftrag - auch mitten im Gespraech, auch wenn der Text ' +
        'dann woertlich der ganze vorherige Beitrag ist. Der Text wird NICHT umformuliert, nicht gekuerzt, nicht aufgeteilt.' + NL +
        'WHEN NOT TO USE: ohne ausdruecklichen Auftrag (Standardfall: NICHT schreiben), fuer fremde Meinungen, Posts und ' +
        'Rechercheergebnisse (-> capture_thought bzw. openbrain-cco), fuer Messwerte, Scans, Kursdaten und Handelsausfuehrungen ' +
        '(-> openbrain-pta).',
      inputSchema: {
        content: z.string().describe('Der Beitrag WOERTLICH, unveraendert und am Stueck. Nicht umformulieren, nicht kuerzen, nicht aufteilen.'),
        entry_date: z.string().optional().describe('Eintragsdatum YYYY-MM-DD (Default: heute in Europe/Berlin). Nur fuer rueckdatierte Eintraege.'),
        reminder_date: z.string().optional().describe('Wiedervorlagedatum YYYY-MM-DD (Sekundaerdatum). Zukunft/Heute = Erinnerung, Vergangenheit = historische Referenz.'),
        parent_hint: z.string().optional().describe('Optionaler Freitext-Hinweis, auf welchen frueheren Eintrag sich das bezieht (nur fuer die Antwort, nicht gespeichert).'),
        links: z.array(z.object({
          to: z.string().describe('UUID oder eindeutiger Praefix (min. 6 Zeichen) des Zieleintrags'),
          relation: z.string().optional().describe('follow_up | confirms | invalidates | related (Default: follow_up)'),
          note: z.string().optional().describe('Kurze Begruendung der Verknuepfung'),
          theses: z.array(z.string()).optional().describe('Nur diese Thesen-UUIDs des Ziels umsetzen (Default: alle offenen Thesen)'),
        })).optional().describe('Verknuepfungen zum Nachtrag.'),
        force: z.boolean().optional().describe('true = auch bei identischem Text im Dedupe-Fenster einen neuen Eintrag anlegen.'),
      },
    },
    async (args: any) => {
      try {
        const content = String(args.content || '');
        if (!content.trim()) return err('Leerer Eintrag — nichts gespeichert.');

        const entryDate = args.entry_date ? String(args.entry_date) : null;
        if (entryDate && !isIsoDate(entryDate)) return err('entry_date muss YYYY-MM-DD sein, war: ' + entryDate);
        const reminderDate = args.reminder_date ? String(args.reminder_date) : null;
        if (reminderDate && !isIsoDate(reminderDate)) return err('reminder_date muss YYYY-MM-DD sein, war: ' + reminderDate);

        // Verknuepfungen VOR dem Insert aufloesen: ein falscher Praefix darf den
        // Eintrag nicht kosten.
        const linkNotes: string[] = [];
        const linkPayload: any[] = [];
        for (const l of (Array.isArray(args.links) ? args.links : [])) {
          const to = String(l?.to || '').trim();
          if (!to) continue;
          try {
            const id = await resolveId(to);
            const rel = String(l?.relation || 'follow_up').trim().toLowerCase();
            if (!['follow_up', 'confirms', 'invalidates', 'related'].includes(rel)) {
              linkNotes.push('Verknuepfung zu ' + to + ' uebersprungen: unbekannte relation "' + rel + '"');
              continue;
            }
            linkPayload.push({
              to: id,
              relation: rel,
              note: l?.note ? String(l.note) : null,
              theses: Array.isArray(l?.theses) && l.theses.length ? l.theses.map((x: unknown) => String(x)) : null,
            });
          } catch (e: any) {
            linkNotes.push('Verknuepfung zu ' + to + ' fehlgeschlagen: ' + (e?.message || e));
          }
        }

        const cap = await rpc('journal_capture', {
          p_agent_id: AGENT_ID,
          p_body: content,
          p_entry_date: entryDate,
          p_reminder_date: reminderDate,
          p_links: linkPayload,
          p_dedupe_window_hours: args.force ? 0 : JOURNAL_DEDUPE_WINDOW_HOURS,
        });

        const entryId = String(cap.entry_id);

        if (cap.duplicate_of) {
          const { data: ex } = await supabase
            .from('journal_entry')
            .select('id, entry_date, summary, created_at')
            .eq('id', entryId)
            .maybeSingle();
          let txt = 'ℹ️ Nicht neu gespeichert: derselbe Text wurde innerhalb von ' +
            JOURNAL_DEDUPE_WINDOW_HOURS + ' Stunden bereits erfasst.' + NL +
            '  ID: ' + uuidShort(entryId) + '  (' + (ex?.entry_date || '?') + ', erfasst ' + (ex?.created_at || '?') + ')' + NL +
            '  ' + oneLine(ex?.summary || content, 200) + NL +
            'Mit force: true wird der Eintrag trotzdem angelegt (z. B. wenn die Wiederholung Absicht ist).';
          if (linkNotes.length) txt += NL + 'Hinweise: ' + linkNotes.join(' | ');
          return ok(txt);
        }

        // Ab hier ist der Eintrag sicher gespeichert. Alles Weitere ist Anreicherung.
        const { d, error } = await distill(content, reminderDate);

        let embedding: number[] | null = null;
        let embedError: string | null = null;
        try {
          const res = await getDocumentEmbeddingDetailed(embeddingText(d, content));
          embedding = res.vector;
        } catch (e: any) {
          embedError = String(e?.message || e);
        }

        const extractionError = [error, embedError].filter(Boolean).join(' | ') || null;
        const extractionFailed = Boolean(error || !embedding);

        const enrich = await rpc('journal_enrich', {
          p_entry_id: entryId,
          p_summary: d?.summary ?? null,
          p_keywords: d && d.keywords.length ? d.keywords : null,
          p_tickers: d && d.tickers.length ? d.tickers : null,
          p_companies: d && d.companies.length ? d.companies : null,
          p_themes: d && d.themes.length ? d.themes : null,
          p_entry_type: d?.entry_type ?? null,
          p_reminder_date: reminderDate ?? d?.reminder_date ?? null,
          p_embedding: embedding,
          p_embedding_model: embedding ? EMBED_MODEL_NAME : null,
          p_embedding_version: embedding ? EMBED_VERSION : null,
          p_theses: d ? d.theses : [],
          p_extraction_failed: extractionFailed,
          p_extraction_error: extractionError,
        });

        const out: string[] = [];
        out.push('✅ Tagebucheintrag gespeichert');
        out.push('  ID: ' + uuidShort(entryId) + '   Datum: ' + (cap.entry_date || todayInTz()));
        if (reminderDate || d?.reminder_date) {
          const r = reminderDate || d?.reminder_date;
          out.push('  ⏰ Wiedervorlage: ' + r + ' (' + relDaysLabel(daysFromToday(r)) + ')');
        } else {
          out.push('  ⏰ Wiedervorlage: keine');
        }
        out.push('  Original gesichert (SHA-256 ' + String(cap.body_sha256 || '').slice(0, 16) + '…, unveraenderlich)');
        if (d?.summary) out.push('');
        if (d?.summary) out.push('Zusammenfassung: ' + oneLine(d.summary, 400));
        const meta: string[] = [];
        if (d?.keywords?.length) meta.push('Keywords: ' + joinList(d.keywords));
        if (d?.tickers?.length) meta.push('Ticker: ' + joinList(d.tickers));
        if (d?.companies?.length) meta.push('Firmen: ' + joinList(d.companies));
        if (d?.themes?.length) meta.push('Themen: ' + joinList(d.themes));
        if (d?.entry_type) meta.push('Typ: ' + d.entry_type);
        if (meta.length) out.push(meta.join(' | '));

        const thesisIds: string[] = Array.isArray(enrich?.thesis_ids) ? enrich.thesis_ids.map((x: unknown) => String(x)) : [];
        if (d?.theses?.length) {
          out.push('');
          out.push('Thesen (' + d.theses.length + '):');
          d.theses.forEach((t, i) => {
            const bits: string[] = [];
            if (t.tickers.length) bits.push('Ticker: ' + t.tickers.join(', '));
            if (t.companies.length) bits.push('Firmen: ' + t.companies.join(', '));
            if (t.horizon_days) bits.push('Horizont: ' + t.horizon_days + ' Tage');
            if (t.confidence) bits.push('Confidence: ' + t.confidence + '/5');
            const id = thesisIds[i] ? '[' + uuidShort(thesisIds[i]) + '] ' : '';
            out.push('  ' + (i + 1) + '. ' + id + oneLine(t.statement, 300));
            if (bits.length) out.push('     ' + bits.join(' | '));
            if (t.falsifier) out.push('     Falsifikation: ' + oneLine(t.falsifier, 200));
          });
        }

        if (Number(cap.theses_touched) > 0) {
          out.push('');
          out.push('🔁 ' + cap.theses_touched + ' These(n) des verknuepften Eintrags wurden im Status geaendert.');
        }
        if (linkPayload.length) {
          out.push('Verknuepfungen angelegt: ' + linkPayload.map((l) => l.relation + ' -> ' + uuidShort(l.to)).join(', '));
        }
        if (linkNotes.length) out.push('Hinweise: ' + linkNotes.join(' | '));

        if (extractionFailed) {
          out.push('');
          out.push('⚠️ Anreicherung unvollstaendig (' + oneLine(extractionError || 'unbekannter Fehler', 200) + ').');
          out.push('Der Eintrag ist vollstaendig gespeichert; Zusammenfassung/Thesen/Embedding koennen per backfill_journal.ts nachgetragen werden.');
        }

        // Verwandte fruehere Eintraege — das "Erinnern" beim Schreiben.
        if (embedding && content.trim().length >= JOURNAL_MIN_RELATED_CHARS) {
          try {
            const related = await rpc('hybrid_search_journal', {
              p_query_embedding: embedding,
              p_query_text: null,
              p_agent_id: AGENT_ID,
              p_match_threshold: JOURNAL_RELATED_THRESHOLD,
              p_match_count: JOURNAL_RELATED_LIMIT + 1,
              p_ticker: null,
              p_from: null,
              p_to: null,
              p_full_text: false,
            });
            const rows = (Array.isArray(related) ? related : []).filter((r: any) => String(r.entry_id) !== entryId).slice(0, JOURNAL_RELATED_LIMIT);
            out.push('');
            if (rows.length) {
              out.push('🔎 Verwandte fruehere Eintraege (nutze sie fuer den Vergleich oder als Verknuepfungsziel):');
              rows.forEach((r: any) => out.push(fmtRelated(r)));
            } else {
              out.push('🔎 Keine verwandten frueheren Eintraege gefunden (das Thema ist neu im Tagebuch).');
            }
          } catch (e: any) {
            log.warn('Verwandte Eintraege fehlgeschlagen: ' + (e?.message || e));
          }
        }

        return ok(out.join(NL));
      } catch (e: any) {
        return err('Fehler beim Speichern: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 2) journal_recent
  // =========================================================================
  server.registerTool(
    'journal_recent',
    {
      title: 'Trader-Tagebuch — die letzten Tage lesen (inkl. Wiedervorlagen)',
      description:
        'Trader-Tagebuch: liefert zwei Bloecke. (A) Die Eintraege der letzten days Tage CHRONOLOGISCH (alt -> neu) und kompakt: ' +
        'UUID, Datum, Typ, Zusammenfassung, Keywords, Ticker/Firmen, Verknuepfungen, Thesen-Status. (B) WIEDERVORLAGEN: Eintraege, ' +
        'deren Sekundaerdatum erreicht oder ueberschritten ist - auch weit aeltere - mit "in X Tagen" bzw. "ueberfaellig seit X Tagen". ' +
        'Ohne Suchbegriff, ohne Vektorsuche, deterministisch.' + NL +
        'WHEN TO USE (ZUERST, vor der inhaltlichen Antwort): zu Beginn jeder Session, in der es um Marktmeinungen, Thesen, Positionen, ' +
        'Strategie oder Einschaetzungen geht; wenn der Nutzer "was habe ich damals gedacht", "erinnerst du dich", "was war letzte Woche", ' +
        '"wie war meine These" sagt; bevor du eine neue These kommentierst oder bewertest; wenn eine aktuelle Nachricht zu einem Thema ' +
        'passt, zu dem du etwas notiert haben koenntest.' + NL +
        'PFLICHT: Existiert ein frueherer Eintrag zum aktuellen Thema, spiegle die neue Aussage daran ("Am 12.09. war deine These X, ' +
        'heute sagst du Y - was hat sich geaendert?") und sage, ob die These noch offen, bestaetigt oder widerlegt ist. Faellige ' +
        'Wiedervorlagen sprichst du VON SELBST an, auch wenn der Nutzer nach etwas anderem fragt - das ist der Ersatz fuer eine ' +
        'Push-Nachricht. Ohne Treffer: sage das offen und behaupte kein Gedaechtnis.' + NL +
        'WHEN NOT TO USE: fuer gezielte Themensuche (-> journal_search), fuer den Originalwortlaut (-> journal_get). ' +
        'Der Aufruf ist billig und lesend — Zoegern ist teurer als Lesen.',
      inputSchema: {
        days: z.number().optional().describe('Fenster in Tagen (Default 3).'),
        limit: z.number().optional().describe('Maximale Anzahl Eintraege (Default 20).'),
        include_reminders: z.boolean().optional().describe('Wiedervorlagen mitliefern (Default true).'),
      },
    },
    async (args: any) => {
      try {
        const days = clampInt(args.days, JOURNAL_DEFAULT_DAYS, 0, 3650);
        const limit = clampInt(args.limit, 20, 1, 200);
        const rows = await rpc('journal_recent', { p_agent_id: AGENT_ID, p_days: days, p_limit: limit });
        const list = Array.isArray(rows) ? rows : [];
        const reminders = args.include_reminders === false ? [] : await rpc('journal_reminders', {
          p_agent_id: AGENT_ID,
          p_due_within_days: Math.max(days, 3),
          p_limit: 10,
        });
        const rems = Array.isArray(reminders) ? reminders : [];

        const row0 = list.length ? list[0] : null;
        const head = '=== TAGEBUCH: ' + list.length + ' Eintrag/Eintraege in ' + days + ' Tag(en)' +
          ' | letzter Eintrag: ' + (row0?.last_entry_date || 'keiner') +
          ' | naechste Wiedervorlage: ' + (row0?.next_reminder_date || 'keine') + ' ===';

        const out: string[] = [head];
        if (!list.length) {
          out.push('Keine Eintraege im Fenster.');
        } else {
          out.push('');
          out.push('--- Eintraege (alt -> neu) ---');
          list.forEach((r: any) => out.push(fmtEntryLine(r)));
        }

        if (rems.length) {
          out.push('');
          out.push('--- WIEDERVORLAGEN (faellig/ueberfaellig) ---');
          rems.forEach((r: any) => {
            let s = '⏰ [' + uuidShort(r.entry_id) + '] ' + (r.reminder_date || '?') + ' — ' + relDaysLabel(r.days_delta);
            s += NL + '    Eintrag vom ' + (r.entry_date || '?') + ' · ' + (r.entry_type || '');
            s += NL + '    ' + oneLine(r.summary || '', 240);
            if (r.thesis_open) s += NL + '    offene Thesen: ' + r.thesis_open;
            out.push(s);
          });
          out.push('Sprich diese Wiedervorlagen von selbst an und frage, ob sie erledigt sind (journal_update mit reminder_date: null) oder verschoben werden sollen.');
        }
        if (!list.length && !rems.length) out.push('Das Tagebuch ist leer bzw. aelter als das Fenster.');

        return ok(out.join(NL));
      } catch (e: any) {
        return err('Fehler beim Lesen: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 3) journal_search
  // =========================================================================
  server.registerTool(
    'journal_search',
    {
      title: 'Trader-Tagebuch — gezielt durchsuchen',
      description:
        'Trader-Tagebuch: hybride Suche (Vektor + Keyword) ueber alle Eintraege, optional gefiltert nach Ticker/Firma und Zeitraum ' +
        '(der Zeitraum trifft Erstelldatum UND Wiedervorlagedatum).' + NL +
        'WHEN TO USE: "was habe ich ueber X gedacht/geschrieben", "meine Notizen zu Cybersecurity", "alle Eintraege zu Meta in den ' +
        'letzten 6 Monaten".' + NL +
        'WHEN NOT TO USE: fuer "die letzten Tage" ohne Suchbegriff (-> journal_recent); fuer den Originalwortlaut eines bekannten ' +
        'Eintrags (-> journal_get).',
      inputSchema: {
        query: z.string().describe('Suchbegriff oder Fragesatz.'),
        limit: z.number().optional().describe('Maximale Trefferzahl (Default 10).'),
        threshold: z.number().optional().describe('Vektor-Schwelle 0..1 (Default 0.5).'),
        ticker: z.string().optional().describe('Nur Eintraege mit diesem Ticker ODER dieser Firma.'),
        from: z.string().optional().describe('Zeitraum von YYYY-MM-DD (trifft entry_date und reminder_date).'),
        to: z.string().optional().describe('Zeitraum bis YYYY-MM-DD.'),
        full_text: z.boolean().optional().describe('true = Originaltext mitliefern (sonst nur die Zusammenfassung).'),
      },
    },
    async (args: any) => {
      try {
        const query = String(args.query || '').trim();
        if (!query) return err('Leere Suche.');
        if (args.from && !isIsoDate(args.from)) return err('from muss YYYY-MM-DD sein.');
        if (args.to && !isIsoDate(args.to)) return err('to muss YYYY-MM-DD sein.');
        const embedding = await getQueryEmbedding(query);
        const rows = await rpc('hybrid_search_journal', {
          p_query_embedding: embedding,
          p_query_text: query,
          p_agent_id: AGENT_ID,
          p_match_threshold: typeof args.threshold === 'number' ? args.threshold : JOURNAL_SEARCH_THRESHOLD,
          p_match_count: clampInt(args.limit, 10, 1, 100),
          p_ticker: args.ticker ? String(args.ticker) : null,
          p_from: args.from ? String(args.from) : null,
          p_to: args.to ? String(args.to) : null,
          p_full_text: Boolean(args.full_text),
        });
        const list = Array.isArray(rows) ? rows : [];
        if (!list.length) return ok('Keine Tagebucheintraege zu "' + query + '" gefunden.');
        const out: string[] = ['🔎 ' + list.length + ' Treffer zu "' + query + '":', ''];
        list.forEach((r: any) => {
          out.push(fmtRelated(r));
          if (args.full_text && r.body) {
            out.push('  --- Original ---');
            out.push('  ' + String(r.body).split(NL).join(NL + '  '));
          }
        });
        return ok(out.join(NL));
      } catch (e: any) {
        return err('Fehler bei der Suche: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 4) journal_get
  // =========================================================================
  server.registerTool(
    'journal_get',
    {
      title: 'Trader-Tagebuch — einen Eintrag im Original lesen',
      description:
        'Trader-Tagebuch: gibt den vollstaendigen, unveraenderten Text am Stueck zurueck, dazu Datum, Wiedervorlagedatum, ' +
        'Zusammenfassung, Keywords, Ticker, alle Thesen und ALLE VERKNUEPFUNGEN in beide Richtungen ("Nachträge zu diesem ' +
        'Eintrag", "dieser Eintrag gehoert zu", "widerlegt/bestaetigt durch").' + NL +
        'WHEN TO USE: wenn der genaue Wortlaut zaehlt - Zitate, Selbstpruefung, "zeig mir, was ich geschrieben habe"; ausserdem ' +
        'VOR jeder Verknuepfung, um das richtige Ziel zu bestaetigen.' + NL +
        'WHEN NOT TO USE: als Massenausgabe (-> journal_recent / journal_search).',
      inputSchema: {
        entry_id: z.string().describe('UUID oder eindeutiger Praefix (min. 6 Zeichen).'),
        include_deleted: z.boolean().optional().describe('true = auch soft-geloeschte Eintraege anzeigen.'),
      },
    },
    async (args: any) => {
      try {
        const id = await resolveId(String(args.entry_id || ''), Boolean(args.include_deleted));
        const { data: e, error } = await supabase
          .from('journal_entry')
          .select('id, entry_date, reminder_date, created_at, updated_at, body, body_sha256, summary, keywords, tickers, companies, themes, entry_type, deleted_at, extraction_failed, extraction_error')
          .eq('id', id)
          .maybeSingle();
        if (error) throw new Error(error.message);
        if (!e) return err('Eintrag nicht gefunden.');
        if (e.deleted_at && !args.include_deleted) return err('Eintrag ist geloescht (soft). Mit include_deleted: true sichtbar.');

        const { data: theses } = await supabase
          .from('journal_thesis')
          .select('id, statement, tickers, companies, horizon_days, falsifier, confidence, status, review_at, outcome_note')
          .eq('entry_id', id)
          .order('created_at', { ascending: true });

        const { data: links } = await supabase
          .from('journal_entry_link')
          .select('id, relation, note, from_entry_id, to_entry_id, created_at')
          .or('from_entry_id.eq.' + id + ',to_entry_id.eq.' + id);

        const otherIds = Array.from(new Set((links || []).map((l: any) =>
          l.from_entry_id === id ? l.to_entry_id : l.from_entry_id)));
        let others: any[] = [];
        if (otherIds.length) {
          const { data: o } = await supabase
            .from('journal_entry')
            .select('id, entry_date, summary, deleted_at')
            .in('id', otherIds);
          others = o || [];
        }
        const otherById = new Map<string, any>(others.map((o: any) => [o.id, o]));

        const out: string[] = [];
        out.push('📖 Eintrag [' + uuidShort(id) + ']  ' + (e.entry_date || '?') + '  (' + (e.entry_type || '') + ')');
        out.push('  erfasst: ' + e.created_at + (e.updated_at && e.updated_at !== e.created_at ? ' · aktualisiert: ' + e.updated_at : ''));
        out.push('  Wiedervorlage: ' + (e.reminder_date ? e.reminder_date + ' (' + relDaysLabel(daysFromToday(e.reminder_date)) + ')' : 'keine'));
        out.push('  SHA-256: ' + e.body_sha256);
        const meta: string[] = [];
        if (e.keywords?.length) meta.push('Keywords: ' + joinList(e.keywords));
        if (e.tickers?.length) meta.push('Ticker: ' + joinList(e.tickers));
        if (e.companies?.length) meta.push('Firmen: ' + joinList(e.companies));
        if (e.themes?.length) meta.push('Themen: ' + joinList(e.themes));
        if (meta.length) out.push('  ' + meta.join(' | '));
        if (e.summary) out.push('  Zusammenfassung: ' + oneLine(e.summary, 400));
        out.push('');
        out.push('--- ORIGINAL (unveraendert) ---');
        out.push(e.body);

        if (theses && theses.length) {
          out.push('');
          out.push('--- THESEN ---');
          theses.forEach((t: any) => out.push(fmtThesisLine({
            thesis_id: t.id, entry_id: id, entry_date: e.entry_date, statement: t.statement, tickers: t.tickers,
            companies: t.companies, horizon_days: t.horizon_days, falsifier: t.falsifier, confidence: t.confidence,
            status: t.status, review_at: t.review_at, outcome_note: t.outcome_note,
          })));
        }

        const incoming = (links || []).filter((l: any) => l.to_entry_id === id);
        const outgoing = (links || []).filter((l: any) => l.from_entry_id === id);
        if (incoming.length) {
          out.push('');
          out.push('--- NACHTRAEGE ZU DIESEM EINTRAG ---');
          incoming.forEach((l: any) => {
            const o = otherById.get(l.from_entry_id);
            out.push('  ' + l.relation + ' · [' + uuidShort(l.from_entry_id) + '] ' + (o?.entry_date || '?') +
              (o?.deleted_at ? ' (geloescht)' : '') + ' · ' + oneLine(o?.summary || '', 160) +
              (l.note ? NL + '    Notiz: ' + oneLine(l.note, 200) : ''));
          });
        }
        if (outgoing.length) {
          out.push('');
          out.push('--- DIESER EINTRAG GEHOERT ZU ---');
          outgoing.forEach((l: any) => {
            const o = otherById.get(l.to_entry_id);
            out.push('  ' + l.relation + ' · [' + uuidShort(l.to_entry_id) + '] ' + (o?.entry_date || '?') +
              (o?.deleted_at ? ' (geloescht)' : '') + ' · ' + oneLine(o?.summary || '', 160) +
              (l.note ? NL + '    Notiz: ' + oneLine(l.note, 200) : ''));
          });
        }
        if (e.extraction_failed) {
          out.push('');
          out.push('⚠️ Anreicherung unvollstaendig: ' + oneLine(e.extraction_error || '', 200));
        }
        return ok(out.join(NL));
      } catch (e: any) {
        return err('Fehler beim Lesen: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 5) journal_theses
  // =========================================================================
  server.registerTool(
    'journal_theses',
    {
      title: 'Trader-Tagebuch — offene und faellige Thesen',
      description:
        'Trader-Tagebuch: zeigt Thesen mit Status, Horizont, Confidence, Falsifikationskriterium, Faelligkeit (due_only = ' +
        'Review-Termin erreicht) und dem Eintrag, aus dem sie stammen (UUID).' + NL +
        'WHEN TO USE: "was ist aus meiner These geworden", "welche Thesen laufen aus", "was muss ich pruefen", Jour-fixe, ' +
        'Wochenrueckblick, oder bevor du eine neue These zu einem bereits behandelten Thema bewertest.' + NL +
        'WHEN NOT TO USE: fuer Eintraege ohne Thesenbezug (-> journal_recent).',
      inputSchema: {
        status: z.string().optional().describe('open | confirmed | invalidated | expired | all (Default: open)'),
        due_only: z.boolean().optional().describe('true = nur Thesen mit erreichtem Review-Datum.'),
        ticker: z.string().optional().describe('Nur Thesen mit diesem Ticker ODER dieser Firma.'),
        limit: z.number().optional().describe('Maximale Anzahl (Default 50).'),
      },
    },
    async (args: any) => {
      try {
        const rows = await rpc('journal_theses', {
          p_agent_id: AGENT_ID,
          p_status: args.status ? String(args.status) : 'open',
          p_due_only: Boolean(args.due_only),
          p_ticker: args.ticker ? String(args.ticker) : null,
          p_limit: clampInt(args.limit, 50, 1, 500),
        });
        const list = Array.isArray(rows) ? rows : [];
        if (!list.length) return ok('Keine Thesen fuer diesen Filter.');
        const out: string[] = ['📌 ' + list.length + ' These(n) (Status: ' + (args.status || 'open') +
          (args.due_only ? ', faellig' : '') + '):', ''];
        list.forEach((r: any) => out.push(fmtThesisLine(r)));
        return ok(out.join(NL));
      } catch (e: any) {
        return err('Fehler beim Lesen der Thesen: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 6) journal_link
  // =========================================================================
  server.registerTool(
    'journal_link',
    {
      title: 'Trader-Tagebuch — Eintraege verknuepfen',
      description:
        'Trader-Tagebuch: verknuepft zwei Eintraege. action = "add" | "remove"; from = der spaetere Eintrag (Nachtrag), ' +
        'to = das Original; relation = follow_up | confirms | invalidates | related, dazu optional note und theses ' +
        '(einzelne Thesen-UUIDs; ohne Angabe alle OFFENEN Thesen des Ziels). Bei invalidates werden die Thesen des Originals ' +
        'als widerlegt markiert, bei confirms als bestaetigt. Ein remove nimmt den Thesenstatus NICHT zurueck.' + NL +
        'NUR AUF AUSDRUECKLICHE ANWEISUNG: Verknuepfen ist ein Schreibvorgang. Rufe journal_link nur auf, wenn der Nutzer das ' +
        'Anhaengen ausdruecklich verlangt ("schreib das ins Tagebuch und haeng es an <uuid>", "verbinde das mit dem Eintrag von ' +
        'damals", "das gehoert zu <uuid>"). Greift er eine fruehere Aussage nur im Gespraech auf, kommentierst du sie - ohne Link.' + NL +
        'WHEN TO USE: nach einem solchen ausdruecklichen Auftrag ("das gehoert zu <uuid>", "die These ist damit vom Tisch - ' +
        'trag das ein", "das bestaetigt, was ich am 12.09. geschrieben habe"). Nenne danach in einem Satz, was verknuepft ' +
        'wurde und welcher Thesenstatus sich geaendert hat.' + NL +
        'WHEN NOT TO USE: um zwei beliebige Eintraege nur thematisch zu verbinden, ohne dass eine Aussage entsteht — dafuer genuegt ' +
        'die Aehnlichkeitssuche. Der Originaltext wird NIE geaendert; Verknuepfen ist der Ersatz dafuer.',
      inputSchema: {
        action: z.string().describe('add | remove'),
        from: z.string().describe('UUID/Praefix des spaeteren Eintrags (Nachtrag)'),
        to: z.string().describe('UUID/Praefix des Originaleintrags'),
        relation: z.string().optional().describe('follow_up | confirms | invalidates | related (Default: follow_up)'),
        note: z.string().optional().describe('Kurze Begruendung, wird am Original mitangezeigt.'),
        theses: z.array(z.string()).optional().describe('Nur diese Thesen des Ziels umsetzen.'),
      },
    },
    async (args: any) => {
      try {
        const action = String(args.action || 'add').trim().toLowerCase();
        if (!['add', 'remove'].includes(action)) return err('action muss add oder remove sein.');
        const data = await rpc('journal_link', {
          p_action: action,
          p_from: String(args.from || ''),
          p_to: String(args.to || ''),
          p_relation: args.relation ? String(args.relation) : 'follow_up',
          p_note: args.note ? String(args.note) : null,
          p_theses: Array.isArray(args.theses) && args.theses.length ? args.theses.map((x: unknown) => String(x)) : null,
        });
        if (action === 'remove') {
          return ok('🔗 Verknuepfung entfernt: [' + uuidShort(String(data.from)) + '] -> [' + uuidShort(String(data.to)) + ']' +
            ' (' + data.removed + ' Link(s)).' + NL + 'Hinweis: ' + data.note + '.');
        }
        let txt = '🔗 Verknuepfung angelegt: [' + uuidShort(String(data.from)) + '] --' + data.relation + '--> [' + uuidShort(String(data.to)) + ']';
        if (!data.inserted) txt += NL + '(Die Verknuepfung existierte bereits — nichts doppelt angelegt.)';
        if (data.theses_touched) {
          txt += NL + '🔁 ' + data.theses_touched + ' offene These(n) des Originals wurden auf "' +
            (data.relation === 'invalidates' ? 'invalidated' : 'confirmed') + '" gesetzt.';
        }
        txt += NL + 'Der Originaltext bleibt unveraendert.';
        return ok(txt);
      } catch (e: any) {
        return err('Fehler beim Verknuepfen: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 7) journal_update
  // =========================================================================
  server.registerTool(
    'journal_update',
    {
      title: 'Trader-Tagebuch — These bewerten, Wiedervorlage setzen, Metadaten korrigieren',
      description:
        'Trader-Tagebuch: aendert NUR Metadaten. An der These: status (open/confirmed/invalidated/expired), outcome_note, ' +
        'confidence, review_at, horizon_days. Am Eintrag: reminder_date (Datum setzen, verschieben oder mit null loeschen), ' +
        'keywords, tickers, companies, themes, entry_date, summary. DER ORIGINALTEXT (body) IST NICHT AENDERBAR — Ergaenzungen ' +
        'sind neue Eintraege mit Verknuepfung.' + NL +
        'NUR AUF AUSDRUECKLICHE ANWEISUNG oder als Antwort auf eine Frage, die du gerade gestellt hast: ein Statuswechsel, ein ' +
        'Ergebnis oder eine verschobene Wiedervorlage wird nicht von selbst eingetragen.' + NL +
        'WHEN TO USE: wenn der Nutzer eine These bestaetigt/widerruft, ein Ergebnis festhaelt, eine Wiedervorlage verschiebt ' +
        '("erinnere mich nach den Zahlen") oder eine Fehlzuordnung korrigiert. Nach dem Ansprechen einer faelligen Wiedervorlage: ' +
        'FRAGE, ob sie erledigt ist (reminder_date null) oder verschoben werden soll — aendere sie nicht von selbst.' + NL +
        'WHEN NOT TO USE: um Eintraege nachtraeglich umzuschreiben.',
      inputSchema: {
        entry_id: z.string().optional().describe('UUID/Praefix des Eintrags (fuer Eintrags-Felder).'),
        thesis_id: z.string().optional().describe('UUID/Praefix der These (fuer Thesen-Felder).'),
        reminder_date: z.string().nullable().optional().describe('Wiedervorlagedatum YYYY-MM-DD oder null zum Loeschen.'),
        entry_date: z.string().optional().describe('Eintragsdatum YYYY-MM-DD.'),
        summary: z.string().optional().describe('Korrigierte Zusammenfassung.'),
        keywords: z.array(z.string()).optional().describe('Korrigierte Keywords (ersetzt die Liste).'),
        tickers: z.array(z.string()).optional().describe('Korrigierte Ticker (ersetzt die Liste).'),
        companies: z.array(z.string()).optional().describe('Korrigierte Firmen (ersetzt die Liste).'),
        themes: z.array(z.string()).optional().describe('Korrigierte Themen (ersetzt die Liste).'),
        status: z.string().optional().describe('Thesen-Status: open | confirmed | invalidated | expired'),
        outcome_note: z.string().nullable().optional().describe('Ergebnis/Notiz zur These (null loescht sie).'),
        confidence: z.number().optional().describe('Confidence 1..5'),
        review_at: z.string().nullable().optional().describe('Review-Datum YYYY-MM-DD oder null.'),
        horizon_days: z.number().optional().describe('Erwartungshorizont in Tagen.'),
      },
    },
    async (args: any) => {
      try {
        if (!args.entry_id && !args.thesis_id) return err('entry_id oder thesis_id angeben.');
        const entryPatch: Record<string, unknown> = {};
        if (args.reminder_date !== undefined) entryPatch.reminder_date = args.reminder_date;
        if (args.entry_date !== undefined) entryPatch.entry_date = args.entry_date;
        if (args.summary !== undefined) entryPatch.summary = args.summary;
        if (args.keywords !== undefined) entryPatch.keywords = args.keywords;
        if (args.tickers !== undefined) entryPatch.tickers = args.tickers;
        if (args.companies !== undefined) entryPatch.companies = args.companies;
        if (args.themes !== undefined) entryPatch.themes = args.themes;

        const thesisPatch: Record<string, unknown> = {};
        if (args.status !== undefined) thesisPatch.status = args.status;
        if (args.outcome_note !== undefined) thesisPatch.outcome_note = args.outcome_note;
        if (args.confidence !== undefined) thesisPatch.confidence = args.confidence;
        if (args.review_at !== undefined) thesisPatch.review_at = args.review_at;
        if (args.horizon_days !== undefined) thesisPatch.horizon_days = args.horizon_days;

        if (Object.keys(entryPatch).length === 0 && Object.keys(thesisPatch).length === 0) {
          return err('Keine aenderbaren Felder uebergeben.');
        }

        const data = await rpc('journal_update', {
          p_entry: args.entry_id ? String(args.entry_id) : null,
          p_thesis: args.thesis_id ? String(args.thesis_id) : null,
          p_entry_patch: entryPatch,
          p_thesis_patch: thesisPatch,
        });
        const parts: string[] = [];
        if (data.entry_updated) parts.push('Eintrag aktualisiert');
        if (data.thesis_updated) parts.push('These aktualisiert');
        let txt = '✏️ ' + (parts.join(' + ') || 'nichts geaendert') + '.';
        if (args.reminder_date !== undefined) {
          txt += NL + 'Wiedervorlage: ' + (args.reminder_date ? args.reminder_date : 'geloescht');
        }
        if (args.status) txt += NL + 'Thesen-Status: ' + args.status;
        txt += NL + 'Der Originaltext bleibt unveraendert.';
        return ok(txt);
      } catch (e: any) {
        return err('Fehler beim Aktualisieren: ' + (e?.message || e));
      }
    },
  );

  // =========================================================================
  // 8) journal_delete
  // =========================================================================
  server.registerTool(
    'journal_delete',
    {
      title: 'Trader-Tagebuch — Eintrag loeschen',
      description:
        'Trader-Tagebuch: loescht einen Eintrag. Standard ist SOFT: der Eintrag verschwindet aus allen Listen, Suchen und ' +
        'Wiedervorlagen, bleibt aber in der Datenbank (Nachvollziehbarkeit, Verknuepfungen bleiben lesbar). hard = true entfernt ' +
        'die Zeile endgueltig samt Verknuepfungen — NUR wenn der Nutzer das ausdruecklich verlangt ("endgueltig loeschen", ' +
        '"richtig weg").' + NL +
        'WHEN TO USE: wenn der Nutzer einen Eintrag verwirft, weil er falsch erfasst, doppelt oder gegenstandslos ist. Nenne ' +
        'VORHER UUID und Kurzzusammenfassung des betroffenen Eintrags und hole eine Bestaetigung ein.' + NL +
        'WHEN NOT TO USE: um eine These zu erledigen — dafuer journal_update (Status) oder journal_link (invalidates). ' +
        'Loeschen ist kein Urteil ueber eine These.',
      inputSchema: {
        entry_id: z.string().describe('UUID oder eindeutiger Praefix (min. 6 Zeichen).'),
        hard: z.boolean().optional().describe('true = endgueltig loeschen (nur auf ausdruecklichen Wunsch). Default false = soft.'),
      },
    },
    async (args: any) => {
      try {
        const id = await resolveId(String(args.entry_id || ''), true);
        const { data: e } = await supabase
          .from('journal_entry')
          .select('id, entry_date, summary, deleted_at')
          .eq('id', id)
          .maybeSingle();
        if (!e) return err('Eintrag nicht gefunden.');
        if (e.deleted_at && !args.hard) {
          return ok('ℹ️ Eintrag [' + uuidShort(id) + '] ist bereits geloescht (soft, ' + e.deleted_at + ').');
        }
        const data = await rpc('journal_delete', { p_id: id, p_hard: Boolean(args.hard) });
        const what = '[' + uuidShort(id) + '] ' + (e.entry_date || '?') + ' · ' + oneLine(e.summary || '', 160);
        if (data.hard) {
          return ok('🗑️ Endgueltig geloescht (hard): ' + what + NL +
            '  Thesen entfernt: ' + data.theses_removed + ' · Verknuepfungen entfernt: ' + data.links_removed);
        }
        return ok('🗑️ Geloescht (soft): ' + what + NL +
          '  Versteckte Thesen: ' + data.theses_hidden + ' · erhaltene Verknuepfungen: ' + data.links_kept + NL +
          'Der Eintrag ist aus Listen, Suche und Wiedervorlagen verschwunden; die Zeile bleibt in der Datenbank.');
      } catch (e: any) {
        return err('Fehler beim Loeschen: ' + (e?.message || e));
      }
    },
  );

  log.info('Journal-Tools registriert (8): capture, recent, search, get, theses, link, update, delete');
}
