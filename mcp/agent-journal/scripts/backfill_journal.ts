// ============================================================================
// mcp/agent-journal/scripts/backfill_journal.ts
//
// Traegt Zusammenfassung, Keywords, Ticker, Thesen und Embedding fuer Eintraege
// nach, bei denen die Anreicherung fehlgeschlagen ist (extraction_failed = true
// oder embedding IS NULL). Ein Tagebucheintrag darf nie durch einen API-Fehler
// verloren gehen — dieses Skript holt die Anreicherung nach.
//
// Aufruf (Host, aus dem QJM-Verzeichnis):
//   deno run -A mcp/agent-journal/scripts/backfill_journal.ts
// Aufruf (Container):
//   docker exec llm-gw-mcp-journal deno run -A /app/scripts/backfill_journal.ts
// ============================================================================
import {
  EMBED_MODEL_NAME,
  EMBED_VERSION,
  getDocumentEmbeddingDetailed,
  log,
  supabase,
} from '../tools/llm.ts';
import { distill, embeddingText } from '../tools/journal_tools.ts';

const LIMIT = parseInt(Deno.env.get('BACKFILL_LIMIT') || '20');

const { data: rows, error } = await supabase
  .from('journal_entry')
  .select('id, body, reminder_date, extraction_failed, embedding')
  .is('deleted_at', null)
  .or('extraction_failed.is.true,embedding.is.null')
  .order('created_at', { ascending: false })
  .limit(LIMIT);

if (error) {
  log.error('Auswahl fehlgeschlagen: ' + error.message);
  Deno.exit(1);
}

if (!rows || rows.length === 0) {
  log.info('Nichts nachzutragen — alle Eintraege sind angereichert.');
  Deno.exit(0);
}

log.info(rows.length + ' Eintrag/Eintraege nachzutragen.');
let ok = 0;
let failed = 0;

for (const row of rows) {
  const id = String(row.id);
  const { d, error: llmError } = await distill(String(row.body), row.reminder_date ? String(row.reminder_date) : null);
  let embedding: number[] | null = null;
  let embedError: string | null = null;
  try {
    const res = await getDocumentEmbeddingDetailed(embeddingText(d, String(row.body)));
    embedding = res.vector;
  } catch (e: any) {
    embedError = String(e?.message || e);
  }

  const extractionError = [llmError, embedError].filter(Boolean).join(' | ') || null;
  const extractionFailed = Boolean(llmError || !embedding);

  const { error: enrichError } = await supabase.rpc('journal_enrich', {
    p_entry_id: id,
    p_summary: d?.summary ?? null,
    p_keywords: d && d.keywords.length ? d.keywords : null,
    p_tickers: d && d.tickers.length ? d.tickers : null,
    p_companies: d && d.companies.length ? d.companies : null,
    p_themes: d && d.themes.length ? d.themes : null,
    p_entry_type: d?.entry_type ?? null,
    p_reminder_date: d?.reminder_date ?? null,
    p_embedding: embedding,
    p_embedding_model: embedding ? EMBED_MODEL_NAME : null,
    p_embedding_version: embedding ? EMBED_VERSION : null,
    p_theses: d ? d.theses : [],
    p_extraction_failed: extractionFailed,
    p_extraction_error: extractionError,
  });

  if (enrichError) {
    failed++;
    log.error(id + ': enrich fehlgeschlagen — ' + enrichError.message);
    continue;
  }

  if (extractionFailed) {
    failed++;
    log.warn(id + ': weiterhin unvollstaendig — ' + (extractionError || 'unbekannt'));
  } else {
    ok++;
    log.info(id + ': nachgetragen (' + (d?.theses?.length || 0) + ' Thesen, Embedding ok)');
  }
}

log.info('Fertig: ' + ok + ' ok, ' + failed + ' weiterhin unvollstaendig.');
log.info('Hinweis: Thesen werden nur angelegt, wenn der Eintrag noch keine hat (p_replace_theses ist hier false).');
