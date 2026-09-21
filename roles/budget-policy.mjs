/**
 * budget-policy.mjs — erzwingt das Workitem-Budget einer Rolle.
 *
 * Wird von den Rollen-Patches gemountet (roles/*.cordis.yml). Haengt am
 * Agent-Scope an agent/pre-step und zaehlt Runden und Tokens.
 *
 * Warum das noetig ist: DSH hat keine Rundengrenze — agent-loop/README.md:200
 * "No built-in turn budget". Ohne diese Policy kann ein Item 151 Runden drehen.
 *
 * Die Zahlen kommen aus dem Session-Log selbst: jedes assistant/message traegt
 * data.usage (inputTokens, outputTokens, totalTokens, cacheReadTokens).
 *
 * Ablauf bei Ueberschreitung:
 *   1. eine letzte Runde  -> {kind:'enter'} mit der Anweisung, SELBST
 *      workitem_finish(status='failed', reason='budget_exceeded') zu rufen
 *   2. danach             -> {kind:'reject'} (kein weiterer Schritt)
 *
 * Der Agent wird also nicht abgeschnitten, sondern schliesst sauber ab. Damit
 * landet der Abbruch als failed im Graphen, die Skip-Kaskade ueberspringt die
 * Geschwister, und das Review (von der Kaskade ausgenommen) bewertet die Luecke.
 */
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import { appendFileSync } from 'node:fs'

// Diagnose-Log. Der DSH-Subprozess laeuft als Kind der Sekretaerin; sein stderr
// landet im Puffer des Python-SDK, nicht im Journal. Deshalb eine Datei.
const LOG = process.env.BUDGET_POLICY_DEBUG
  ? '/home/daniel/QJM/services/secretary/budget-policy.log'
  : null
function dbg(msg) {
  if (LOG === null) return
  try {
    appendFileSync(LOG, new Date().toISOString() + ' ' + msg + '\n')
  } catch { /* Log darf nie werfen */ }
}

export const name = 'budget-policy'

/** Fallback, wenn der Lead kein Budget gesetzt hat — grosszuegig. */
const DEFAULT_BUDGET = { rounds: 25, tokens: 600000 }

function textOf(message) {
  const content = message?.content
  if (!Array.isArray(content)) return ''
  return content.filter((b) => b?.type === 'text').map((b) => b.text ?? '').join('\n')
}

/** Liest die budget_json-Zeile aus der ersten User-Nachricht der Session. */
function parse(text) {
  const match = /^budget_json:\s*(\{.*\})\s*$/m.exec(text)
  if (!match) return null
  try {
    return { ...DEFAULT_BUDGET, ...JSON.parse(match[1]) }
  } catch {
    return { ...DEFAULT_BUDGET }
  }
}

function readBudget(agent, pending) {
  // 1. die Nachrichten, die gerade in diesen Schritt wollen
  for (const message of pending ?? []) {
    const found = parse(textOf(message))
    if (found) return found
  }
  // 2. sonst die Session absuchen (Folgeschritte)
  for (const event of agent.session.snapshotEvents()) {
    if (event.type !== 'user/message') continue
    const found = parse(textOf(event.data?.message))
    if (found) return found
  }
  return { ...DEFAULT_BUDGET }
}

/** Verbrauch aus dem Session-Log falten — nur neue Events, kein O(n^2). */
function makeUsageReader(agent) {
  let lastSeq = -1
  let tokens = 0
  let rounds = 0
  return () => {
    for (const event of agent.session.snapshotEvents()) {
      if (event.seq <= lastSeq) continue
      lastSeq = event.seq
      if (event.type !== 'assistant/message') continue
      if (typeof event.data?.step === 'number') rounds = Math.max(rounds, event.data.step)
      const usage = event.data?.usage
      if (!usage) continue
      tokens += typeof usage.totalTokens === 'number'
        ? usage.totalTokens
        : (usage.inputTokens ?? 0) + (usage.cacheReadTokens ?? 0)
          + (usage.cacheWriteTokens ?? 0) + (usage.outputTokens ?? 0)
    }
    return { tokens, rounds }
  }
}

function overBudget(budget, usage) {
  const breaches = []
  if (budget.rounds > 0 && usage.rounds > budget.rounds) breaches.push('rounds')
  if (budget.tokens > 0 && usage.tokens > budget.tokens) breaches.push('tokens')
  return breaches
}

function budgetMessage(breaches, budget, usage) {
  return [
    '[BUDGET ERSCHOEPFT]',
    'Verbraucht: ' + JSON.stringify(usage),
    'Budget:     ' + JSON.stringify(budget),
    'Gerissen:   ' + breaches.join(', '),
    '',
    'Du bekommst GENAU DIESE EINE letzte Runde. Nutze sie ausschliesslich dafuer,',
    'das Workitem sauber abzuschliessen:',
    '',
    '  workitem_finish(status="failed", result={',
    '    "reason": "budget_exceeded",',
    '    "breached": [' + breaches.map((b) => '"' + b + '"').join(', ') + '],',
    '    "usage": ' + JSON.stringify(usage) + ',',
    '    "budget": ' + JSON.stringify(budget) + ',',
    '    "achieved": "<was du bis hierhin erreicht hast>",',
    '    "needs": "<was ein erneuter Versuch braeuchte, oder: nicht loesbar>"',
    '  })',
    '',
    'Rufe KEINE weiteren Tools mehr auf.',
  ].join('\n')
}

export function apply(ctx) {
  dbg('apply: plugin geladen')
  ctx.on('agent/created', ({ agent }) => {
    dbg('agent/created id=' + agent.id)
    const readUsage = makeUsageReader(agent)
    let budget = null
    let warned = false

    agent.ctx.on('agent/pre-step', (payload, next) => {
      // Erst hier lesen: bei agent/created ist die erste User-Nachricht noch
      // nicht in der Session, readBudget faende nichts und naehme die Defaults.
      if (budget === null) {
        budget = readBudget(agent, payload.messages)
        const first = (payload.messages ?? [])[0]
        dbg('erste pre-step: budget=' + JSON.stringify(budget)
            + ' pendingMessages=' + (payload.messages ?? []).length
            + ' rolle=' + (first?.role ?? '?')
            + ' text=' + JSON.stringify(textOf(first)).slice(0, 400))
        const snapshot = agent.session.snapshotEvents().filter((e) => e.type === 'user/message')
        dbg('user/messages in der Session: ' + snapshot.length
            + ' | erste=' + JSON.stringify(textOf(snapshot[0]?.data?.message)).slice(0, 300))
      }
      const usage = readUsage()
      dbg('pre-step ' + payload.step + ' usage=' + JSON.stringify(usage))
      // payload.step ist die Runde, die JETZT beginnt. Ohne diese Zeile zaehlt
      // die Policy nur abgeschlossene Runden und erlaubt eine Runde zu viel.
      usage.rounds = Math.max(usage.rounds, payload.step ?? 0)
      const breaches = overBudget(budget, usage)
      if (breaches.length === 0) return next()

      if (!warned) {
        warned = true
        const message = createUserMessage({
          content: [{ type: 'text', text: budgetMessage(breaches, budget, usage) }],
          source: { kind: 'plugin', plugin: 'budget-policy', form: 'notice', summary: 'budget exhausted' },
        })
        return { kind: 'enter', messages: [...(payload.messages ?? []), message] }
      }
      return { kind: 'reject' }
    })
  })
}
