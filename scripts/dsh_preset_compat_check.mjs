#!/usr/bin/env node
// ============================================================================
// dsh_preset_compat_check.mjs — Prueft, ob alle Pakete, die eine Agent-Preset-
// Komposition referenziert, in einer Zielversion von DSH vorhanden sind.
//
// Hintergrund: 0.1.7 hat den PTC-/Workflow-Zweig umbenannt (ptc-runtime-Familie
// ohne Legacy-Aliase) und Agent-Presets von Verzeichnis-Roots auf
// Plugin-Rows (@deepseek-ai/dsh-agent-preset) umgestellt. Eine Komposition,
// die ein entferntes Paket nennt, laedt nicht mehr.
//
// Aufruf:
//   node dsh_preset_compat_check.mjs <preset.yml> [--version 0.1.7-rc.2]
//                                                  [--source npm|local]
//                                                  [--checkout /home/daniel/deepseek-harness]
//   --source npm    (Default) fragt registry.npmjs.org nach <version>
//   --source local  prueft die Pakete im Checkout (nach 'pnpm install')
//
// Exit-Code: 0 = alle Referenzen vorhanden, 1 = mindestens eine fehlt.
// ============================================================================

import { readFileSync } from 'node:fs'
import { resolve, join } from 'node:path'

const args = process.argv.slice(2)
const file = args.find(a => !a.startsWith('--'))
const opt = (name, fallback) => {
  const i = args.indexOf('--' + name)
  return i >= 0 && args[i + 1] !== undefined ? args[i + 1] : fallback
}
const version = opt('version', '0.1.7-rc.2')
const source = opt('source', 'npm')
const checkout = opt('checkout', '/home/daniel/deepseek-harness')

if (file === undefined) {
  console.error('usage: dsh_preset_compat_check.mjs <preset.yml> [--version X] [--source npm|local]')
  process.exit(2)
}

const text = readFileSync(resolve(file), 'utf8')
// Nur Zeilen der Form  name: '@scope/pkg'  bzw.  name: cordis:group  auswerten.
const refs = [...new Set([...text.matchAll(/^\s*(?:-\s*)?name:\s*'([^']+)'/gm)].map(m => m[1]))]
const packages = refs
  .filter(r => r.startsWith('@') || (!r.includes(':') && r.includes('/')))
  // Subpfad-Exporte wie '@scope/pkg/sub/pfad' auf das Paket '@scope/pkg' zurueckfuehren
  .map(r => (r.startsWith('@') ? r.split('/').slice(0, 2).join('/') : r.split('/')[0]))
  .filter((v, i, a) => a.indexOf(v) === i)
const nonPackages = refs.filter(r => !packages.includes(r))

console.log('Preset   :', resolve(file))
console.log('Version  :', version, '(source: ' + source + ')')
console.log('Referenzen:', refs.length, 'davon Pakete:', packages.length, '| Nicht-Paket-Rows:', nonPackages.join(', ') || '-')
console.log('')

async function npmHas(name, want) {
  const url = 'https://registry.npmjs.org/' + name.replace('/', '%2f')
  const res = await fetch(url, { signal: AbortSignal.timeout(15000) })
  if (!res.ok) return { ok: false, detail: 'HTTP ' + res.status }
  const body = await res.json()
  const versions = Object.keys(body.versions ?? {})
  if (versions.includes(want)) return { ok: true, detail: want }
  const near = versions.filter(v => v.startsWith(want.split('-')[0])).slice(-1)[0]
  return { ok: false, detail: near ? 'nur ' + near : 'keine ' + want.split('-')[0] + '-Version' }
}

async function localHas(name) {
  // Nach 'pnpm install' loest der Checkout jedes Workspace-Paket ueber
  // node_modules auf; fehlt der Ordner, ist das Paket nicht mehr Teil des Trees.
  const candidates = [
    join(checkout, 'node_modules', ...name.split('/')),
    join(checkout, 'apps/cli/node_modules', ...name.split('/')),
  ]
  for (const c of candidates) {
    try { const st = await import('node:fs').then(m => m.statSync(c)); if (st.isDirectory() || st.isSymbolicLink()) return { ok: true, detail: c } } catch {}
  }
  return { ok: false, detail: 'nicht in node_modules' }
}

let missing = 0
for (const p of packages) {
  const r = source === 'local' ? await localHas(p) : await npmHas(p, version)
  if (!r.ok) missing++
  console.log((r.ok ? 'OK    ' : 'FEHLT ') + p.padEnd(52) + ' ' + r.detail)
}
console.log('')
console.log(missing === 0
  ? 'Ergebnis: alle Referenzen vorhanden.'
  : 'Ergebnis: ' + missing + ' Referenz(en) fehlen -> Komposition VOR dem Mounten anpassen.')
process.exit(missing === 0 ? 0 : 1)
