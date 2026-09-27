// ============================================================================
// mcp/agent-journal/tools/llm.ts
//
// Minimale Helfer-Kopie aus mcp/agent-cco/tools/shared.ts (bewusste Duplikation,
// siehe TRADER_JOURNAL_MCP_PLAN.md Abschnitt 11): Supabase-Client, Embeddings
// ueber Switchyard (mxbai-embed-large, 1024 Dim) und DeepSeek-Chat.
// NICHT die bestehenden Server anfassen.
// ============================================================================
import { createClient } from '@supabase/supabase-js';

// --- Identitaet / Konfiguration -------------------------------------------
export const AGENT_ID = Deno.env.get('AGENT_ID') || 'journal';
export const MCP_ACCESS_KEY = Deno.env.get('MCP_ACCESS_KEY') || '';
export const JOURNAL_TZ = Deno.env.get('JOURNAL_TZ') || 'Europe/Berlin';
export const JOURNAL_DEDUPE_WINDOW_HOURS = parseInt(Deno.env.get('JOURNAL_DEDUPE_WINDOW_HOURS') || '12');
export const JOURNAL_RELATED_LIMIT = parseInt(Deno.env.get('JOURNAL_RELATED_LIMIT') || '3');
export const JOURNAL_RELATED_THRESHOLD = parseFloat(Deno.env.get('JOURNAL_RELATED_THRESHOLD') || '0.5');
export const JOURNAL_SEARCH_THRESHOLD = parseFloat(Deno.env.get('JOURNAL_SEARCH_THRESHOLD') || '0.5');
export const JOURNAL_DEFAULT_DAYS = parseInt(Deno.env.get('JOURNAL_DEFAULT_DAYS') || '3');
export const JOURNAL_MIN_RELATED_CHARS = parseInt(Deno.env.get('JOURNAL_MIN_RELATED_CHARS') || '40');

// --- Supabase --------------------------------------------------------------
export const SUPABASE_URL = Deno.env.get('SUPABASE_URL') || 'http://127.0.0.1:8001';
export const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get('SUPABASE_SERVICE_ROLE_KEY') || '';
export const supabase = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, {
  auth: { persistSession: false },
});

// --- Embeddings (Switchyard) ----------------------------------------------
export const SWITCHYARD_URL = Deno.env.get('SWITCHYARD_URL') || 'http://switchyard:4000/v1';
export const EMBED_MODEL = Deno.env.get('EMBED_MODEL') || 'embeddings';
export const EMBED_DIM = parseInt(Deno.env.get('EMBED_DIM') || '1024');
export const EMBED_MAX_CHARS = parseInt(Deno.env.get('EMBED_MAX_CHARS') || '1100');
export const EMBED_VERSION = Deno.env.get('EMBED_VERSION') || 'mxbai-v1';
export const EMBED_MODEL_NAME = Deno.env.get('EMBED_MODEL_NAME') || 'mxbai-embed-large';
export const EMBED_QUERY_PREFIX = Deno.env.get('EMBED_QUERY_PREFIX') ||
  'Represent this sentence for searching relevant passages: ';
export const EMBED_SHRINK_BUDGETS = (Deno.env.get('EMBED_SHRINK_BUDGETS') || '1100,800,560,380,240,140')
  .split(',').map((s) => parseInt(s.trim())).filter((n) => Number.isFinite(n) && n > 0);

// Erlaubte Prioritaetsklassen des Gateways (unbekannte Klasse = harter Fehler).
export type EmbeddingPriority = 'x_search' | 'x_post' | 'yt';

// --- DeepSeek --------------------------------------------------------------
const DEEPSEEK_API_KEY = Deno.env.get('DEEPSEEK_API_KEY') || '';
const DEEPSEEK_BASE_URL = (Deno.env.get('DEEPSEEK_BASE_URL') || 'https://api.deepseek.com').replace(/[/]+$/, '');
const DEEPSEEK_MODEL = Deno.env.get('DEEPSEEK_MODEL') || 'deepseek-flash';
const DEEPSEEK_MAX_RETRIES = parseInt(Deno.env.get('DEEPSEEK_MAX_RETRIES') || '3');
const DEEPSEEK_RETRY_BASE_MS = parseInt(Deno.env.get('DEEPSEEK_RETRY_BASE_MS') || '1500');
const DEEPSEEK_REASONING_EFFORT = (Deno.env.get('DEEPSEEK_REASONING_EFFORT') || 'off').toLowerCase();

const JOURNAL_PROMPT_PATHS = [
  '/app/prompts/journal-prompt.txt',
  'mcp/agent-journal/prompts/journal-prompt.txt',
  'prompts/journal-prompt.txt',
  'journal-prompt.txt',
];

export const log = {
  info: (m: string) => console.log('[journal] ' + m),
  warn: (m: string) => console.warn('[journal] ' + m),
  error: (m: string) => console.error('[journal] ' + m),
  debug: (m: string) => {
    if (Deno.env.get('JOURNAL_DEBUG') === '1') console.log('[journal:debug] ' + m);
  },
};

export class LlmUnavailableError extends Error {
  readonly retryable: boolean;
  constructor(message: string, retryable = true) {
    super(message);
    this.name = 'LlmUnavailableError';
    this.retryable = retryable;
  }
}

export function loadJournalPrompt(): string {
  for (const p of JOURNAL_PROMPT_PATHS) {
    try {
      return Deno.readTextFileSync(p);
    } catch (_e) {
      // naechster Pfad
    }
  }
  log.warn('journal-prompt.txt nicht gefunden — verwende Minimal-Fallback');
  return 'Du bist ein Analyst fuer ein Trader-Tagebuch. Antworte AUSSCHLIESSLICH mit validem JSON.';
}

// --- Text-Helfer -----------------------------------------------------------

/** Kuerzt einen Dokumenttext an der letzten Wortgrenze (idempotent). */
export function fitForEmbedding(text: string, maxChars: number = EMBED_MAX_CHARS): string {
  if (typeof text !== 'string' || text.length === 0) return '';
  if (maxChars <= 0 || text.length <= maxChars) return text;
  const head = text.slice(0, maxChars);
  const cut = Math.max(head.lastIndexOf(' '), head.lastIndexOf(String.fromCharCode(10)), head.lastIndexOf(String.fromCharCode(9)));
  return (cut > maxChars * 0.5 ? head.slice(0, cut) : head).trimEnd();
}

export function isContextLengthError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err ?? '');
  return /context length|context window|exceeds the context/i.test(msg);
}

export async function sha256Hex(text: string): Promise<string> {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Heutiges Datum (YYYY-MM-DD) in der Journal-Zeitzone. */
export function todayInTz(tz: string = JOURNAL_TZ): string {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
}

/** Kurzform einer UUID fuer kompakte Ausgaben (erste 8 Zeichen). */
export function uuidShort(id: string | null | undefined): string {
  return (id || '').slice(0, 8);
}

export function isIsoDate(s: string | null | undefined): boolean {
  if (!s) return false;
  if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(s)) return false;
  const d = new Date(s + 'T00:00:00Z');
  return !Number.isNaN(d.getTime());
}

/** Einzeilige, laengenbegrenzte Darstellung fuer kompakte Tool-Ausgaben. */
export function oneLine(s: string | null | undefined, max = 240): string {
  const t = (s || '').replace(/[ \t\r\n]+/g, ' ').trim();
  return t.length > max ? t.slice(0, max - 1) + '…' : t;
}

export function joinList(arr: string[] | null | undefined, fallback = '-'): string {
  if (!arr || arr.length === 0) return fallback;
  return arr.join(', ');
}

// --- Embeddings ------------------------------------------------------------

async function embedBatch(
  texts: string[],
  priority: EmbeddingPriority,
): Promise<{ vectors: number[][]; model: string | null }> {
  if (!texts || texts.length === 0) return { vectors: [], model: null };
  const baseUrl = SWITCHYARD_URL.endsWith('/v1') ? SWITCHYARD_URL : SWITCHYARD_URL + '/v1';
  const r = await fetch(baseUrl + '/embeddings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer switchyard' },
    body: JSON.stringify({ model: EMBED_MODEL, input: texts, priority }),
  });
  if (!r.ok) {
    const errText = await r.text();
    throw new Error('Embeddings failed (' + baseUrl + '/embeddings, class ' + priority + ', status ' + r.status + '): ' + errText);
  }
  const d = await r.json();
  const vectors = Array.isArray(d?.data) ? d.data.map((item: any) => item.embedding) : [];
  const reportedModel = typeof d?.model === 'string' ? d.model : null;
  for (let i = 0; i < vectors.length; i++) {
    const v = vectors[i];
    if (!Array.isArray(v) || v.length !== EMBED_DIM) {
      throw new Error('Embedding-Dimension ' + (Array.isArray(v) ? v.length : '?') + ' != ' + EMBED_DIM +
        ' (Index ' + i + ', Modell ' + (reportedModel || '?') + ') — falsche Gateway-Route oder Modellwechsel ohne Re-Embed.');
    }
  }
  return { vectors, model: reportedModel };
}

/**
 * Dokument-Embedding mit Garantie: faengt die 512-Token-Kontextgrenze des Backends
 * ab und verkleinert schrittweise. Liefert den tatsaechlich eingebetteten Text mit.
 */
export async function getDocumentEmbeddingDetailed(
  text: string,
): Promise<{ vector: number[] | null; model: string | null; text: string }> {
  const fitted = fitForEmbedding(text);
  if (!fitted) return { vector: null, model: null, text: '' };
  try {
    const { vectors, model } = await embedBatch([fitted], 'x_post');
    return { vector: vectors[0], model, text: fitted };
  } catch (err) {
    if (!isContextLengthError(err)) throw err;
    log.warn('Embedding-Backend meldet Kontextgrenze — verkleinere schrittweise.');
  }
  for (const budget of EMBED_SHRINK_BUDGETS) {
    const candidate = fitForEmbedding(fitted, budget);
    if (!candidate) continue;
    try {
      const { vectors, model } = await embedBatch([candidate], 'x_post');
      return { vector: vectors[0], model, text: candidate };
    } catch (err) {
      if (!isContextLengthError(err)) throw err;
    }
  }
  throw new Error('Text auch mit dem kleinsten Budget nicht einbettbar.');
}

export async function getQueryEmbedding(query: string): Promise<number[]> {
  const { vectors } = await embedBatch([EMBED_QUERY_PREFIX + query], 'x_search');
  return vectors[0];
}

// --- DeepSeek --------------------------------------------------------------

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function thinkingPayload(effort: string): Record<string, unknown> {
  switch (effort) {
    case 'off':
    case 'disabled':
      return { thinking: { type: 'disabled' } };
    case 'low':
    case 'high':
    case 'max':
      return { thinking: { type: 'enabled' }, reasoning_effort: effort };
    default:
      return {};
  }
}

export async function deepseekChat(payload: Record<string, unknown>): Promise<any> {
  if (!DEEPSEEK_API_KEY) throw new LlmUnavailableError('DEEPSEEK_API_KEY ist nicht gesetzt', false);
  const url = DEEPSEEK_BASE_URL + '/chat/completions';
  let lastErr = 'unbekannter Fehler';
  for (let attempt = 0; attempt <= DEEPSEEK_MAX_RETRIES; attempt++) {
    let res: Response | null = null;
    try {
      res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + DEEPSEEK_API_KEY },
        body: JSON.stringify(payload),
      });
    } catch (e: any) {
      lastErr = 'Netzwerkfehler: ' + (e?.message || e);
      if (attempt < DEEPSEEK_MAX_RETRIES) {
        await sleep(DEEPSEEK_RETRY_BASE_MS * Math.pow(2, attempt));
        continue;
      }
      throw new LlmUnavailableError(lastErr, true);
    }
    if (res.ok) return await res.json();
    const body = await res.text().catch(() => '');
    if (res.status === 401 || res.status === 402 || res.status === 403) {
      throw new LlmUnavailableError('DeepSeek HTTP ' + res.status + ': ' + body, false);
    }
    if (res.status === 429 || res.status >= 500) {
      lastErr = 'DeepSeek HTTP ' + res.status + ': ' + body;
      const retryAfterS = Number(res.headers.get('retry-after')) || 0;
      const wait = Math.max(retryAfterS * 1000, DEEPSEEK_RETRY_BASE_MS * Math.pow(2, attempt));
      if (attempt < DEEPSEEK_MAX_RETRIES) {
        log.warn(lastErr + ' — Retry ' + (attempt + 1) + '/' + DEEPSEEK_MAX_RETRIES + ' in ' + Math.round(wait) + 'ms');
        await sleep(wait);
        continue;
      }
      throw new LlmUnavailableError(lastErr, true);
    }
    throw new LlmUnavailableError('DeepSeek HTTP ' + res.status + ': ' + body, false);
  }
  throw new LlmUnavailableError(lastErr, true);
}

export async function deepseekChatCompletion(
  messages: Array<Record<string, unknown>>,
  opts: { temperature?: number; maxTokens?: number; reasoningEffort?: string } = {},
): Promise<string> {
  const effort = (opts.reasoningEffort ?? DEEPSEEK_REASONING_EFFORT).toLowerCase();
  const data = await deepseekChat({
    model: DEEPSEEK_MODEL,
    messages,
    temperature: opts.temperature ?? 0.1,
    ...thinkingPayload(effort),
    ...(opts.maxTokens ? { max_tokens: opts.maxTokens } : {}),
  });
  return data?.choices?.[0]?.message?.content || '';
}

/** Schneidet das erste JSON-Objekt aus einer LLM-Antwort (ohne Regex-Backslashes). */
export function extractJsonObject(content: string): any | null {
  if (!content) return null;
  const start = content.indexOf('{');
  const end = content.lastIndexOf('}');
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(content.slice(start, end + 1));
  } catch (_e) {
    return null;
  }
}
