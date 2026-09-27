#!/usr/bin/env node
// ============================================================================
// dsh_preset_migrate.mjs — migriert ein Legacy-Verzeichnis-Preset
// (<root>/<id>/{preset.yml,agent.cordis.yml}) in die DSH-0.1.7-Form:
// eine @deepseek-ai/dsh-agent-preset-Row in der Profil-Patchdatei.
//
// WARUM: In 0.1.7 ersetzt @deepseek-ai/dsh-agent-preset-registry das alte
// @deepseek-ai/dsh-agent-presets. Der Registry-README: "The registry neither
// scans directories nor accepts preset paths." Das roots-Konzept entfaellt.
// Ausserdem umbenannt (ptc-runtime-Familie, keine Legacy-Aliase):
//   @deepseek-ai/dsh-workflow-worker-thread  ->  @deepseek-ai/dsh-ptc-runtime-node
//                                             +  @deepseek-ai/dsh-workflow-ptc
//
// Das Skript SCHREIBT NICHTS in die Installation. Es erzeugt:
//   <out>/cordis.patch.candidate.yml     Kandidat fuer ~/.dsh/profiles/web/cordis.patch.yml
//   <out>/preset-<id>.composition.yml    nur die plugins:-Liste
//   <out>/MIGRATION_NOTES.md
//
// Aufruf:
//   node dsh_preset_migrate.mjs --preset-dir /home/daniel/QJM/agent-presets/trader \
//     --out /home/daniel/QJM/dsh_playground/dsh_update_0.1.7 [--preset-id trader] [--order 10] [--default]
//   --default  setzt zusaetzlich default: <preset-id> in der agent-preset-registry-Row
// ============================================================================

import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs'
import { resolve, join, basename } from 'node:path'

const argv = process.argv.slice(2)
const opt = (name, fallback) => {
  const i = argv.indexOf('--' + name)
  return i >= 0 && argv[i + 1] !== undefined ? argv[i + 1] : fallback
}
const flag = (name) => argv.includes('--' + name)

const presetDir = resolve(opt('preset-dir', '/home/daniel/QJM/agent-presets/trader'))
const outDir = resolve(opt('out', '/home/daniel/QJM/dsh_playground/dsh_update_0.1.7'))
const presetId = opt('preset-id', basename(presetDir))
const order = opt('order', '10')
// Registry-Default: standard = sicherer erster Boot; --default setzt das Preset selbst.
const registryDefaultId = flag('default') ? presetId : opt('registry-default', 'standard')

const compositionPath = join(presetDir, 'agent.cordis.yml')
const metaPath = join(presetDir, 'preset.yml')
if (!existsSync(compositionPath)) {
  console.error('FEHLER: ' + compositionPath + ' nicht gefunden.')
  process.exit(2)
}

// --- Metadaten (preset.yml) -------------------------------------------------
let title = ''
let description = ''
if (existsSync(metaPath)) {
  const meta = readFileSync(metaPath, 'utf8')
  title = (meta.match(/^name:\s*(.+)$/m)?.[1] ?? '').trim().replace(/^['"]|['"]$/g, '')
  description = (meta.match(/^description:\s*(.+)$/m)?.[1] ?? '').trim().replace(/^['"]|['"]$/g, '')
}

// --- Komposition lesen und Row-Umbenennungen anwenden ----------------------
let body = readFileSync(compositionPath, 'utf8')
const renames = []

const wwt = /^([ \t]*)- id: workflow-worker-thread[ \t]*\r?\n\1  name: '@deepseek-ai\/dsh-workflow-worker-thread'[^\n]*\r?\n(\1  config:[ \t]*\r?\n\1    provider: spawn[ \t]*\r?\n)?/m
if (wwt.test(body)) {
  // WICHTIG: ptc-runtime gehoert in den HOST-Plan (Basis-Bundle). Im Agent-Preset
  // wuerde die Row denselben Dienst ein zweites Mal registrieren -> Mount schlaegt
  // fehl ("Failed to load"). Im Preset bleibt nur workflow-ptc (wie im mitgelieferten
  // standard-Preset). Fuer run_code im Agenten stattdessen tool-presentation
  // (@deepseek-ai/dsh-agent-tool-presentation, mode: ptc|both) ergaenzen.
  body = body.replace(wwt, (_m, ind) =>
    ind + '- id: workflow-ptc\n' +
    ind + "  name: '@deepseek-ai/dsh-workflow-ptc'\n" +
    ind + '  config:\n' +
    ind + '    provider: spawn\n\n' +
    // PTC-Praesentation liegt in 0.1.7 im Agent-Preset (Default waere 'native' = kein run_code).
    ind + '- id: tool-presentation\n' +
    ind + "  name: '@deepseek-ai/dsh-agent-tool-presentation'\n" +
    ind + '  config:\n' +
    ind + '    mode: both\n\n' +
    ind + '- id: present\n' +
    ind + "  name: '@deepseek-ai/dsh-tool-present'\n")
  renames.push('workflow-worker-thread -> workflow-ptc + tool-presentation(mode: both) + present')
}

// --- plugins:-Liste erzeugen (gesamten Block um 10 Leerzeichen einruecken) --
const INDENT = ' '.repeat(10)
const composition = body
  .replace(/\r\n/g, '\n')
  .split('\n')
  .map((line) => (line.trim() === '' ? '' : INDENT + line))
  .join('\n')
  .replace(/\s+$/, '')

const headerComment = body.split('\n').filter((l) => l.startsWith('#')).join('\n')

const registryDefault =
  '- id: agent-preset-registry\n  config:\n    default: ' + registryDefaultId + '\n\n'

const banner = '# ' + '='.repeat(74) + '\n' +
  '# KANDIDAT fuer ~/.dsh/profiles/web/cordis.patch.yml (DSH 0.1.7-rc.2)\n' +
  '# Generiert aus ' + compositionPath + '\n' +
  '# NICHT automatisch installiert - erst reviewen, dann kopieren.\n' +
  '# ' + '='.repeat(74) + '\n\n'

const patch =
  banner +
  (headerComment ? '# Kopfkommentar der Ursprungsdatei (Historik):\n' +
    headerComment.split('\n').map((l) => '# ' + l.replace(/^#\s?/, '')).join('\n') + '\n\n' : '') +
  registryDefault +
  '- insert:\n' +
  '    - id: preset-' + presetId + '\n' +
  "      name: '@deepseek-ai/dsh-agent-preset'\n" +
  '      config:\n' +
  '        id: ' + presetId + '\n' +
  (description ? '        description: ' + JSON.stringify(description) + '\n' : '') +
  '        order: ' + order + '\n' +
  '        plugins:\n' +
  composition + '\n'

mkdirSync(outDir, { recursive: true })
writeFileSync(join(outDir, 'cordis.patch.candidate.yml'), patch)
writeFileSync(join(outDir, 'preset-' + presetId + '.composition.yml'), composition + '\n')

const notes = [
  '# Preset-Migration ' + presetId + ' -> DSH 0.1.7-rc.2',
  '',
  '- Quelle: ' + compositionPath,
  '- Ziel-Row: preset-' + presetId + ' (@deepseek-ai/dsh-agent-preset)',
  '- Registry-Default in dieser Variante: ' + registryDefaultId + (registryDefaultId === presetId ? ' (Preset aktiv)' : ' (Sicherer erster Boot; fuer den Endzustand mit --default neu erzeugen)'),
  title ? '- Alter Anzeigename (preset.yml name): "' + title + '" - hat in 0.1.7 kein Feld; die UI zeigt die Preset-ID.' : '- Kein preset.yml-Name gefunden.',
  description ? '- Beschreibung uebernommen.' : '- Keine Beschreibung gefunden.',
  '',
  '## Automatische Row-Umbenennungen',
  ...(renames.length > 0 ? renames.map((r) => '- ' + r) : ['- keine']),
  '',
  '## Review-Schritte vor dem Kopieren',
  '1. Kompatibilitaet: node scripts/dsh_preset_compat_check.mjs <composition> --version 0.1.7-rc.2',
  '2. Nach dem Update Schema ziehen: dsh --profile web --dump-config-schema > /tmp/schema.json',
  '3. Erst mit default: standard booten, danach den Kandidaten nach ~/.dsh/profiles/web/cordis.patch.yml kopieren.',
  '4. dsh --profile web --dump-config muss die Row preset-' + presetId + ' zeigen.',
  '',
  '## Nicht automatisch gepruefte Punkte',
  '- Config-Felder einzelner Rows koennen sich zwischen 0.1.5 und 0.1.7 geaendert haben (z. B. tool-subagent, persona).',
  '  Beim ersten Boot mit dem Kandidaten das Journal auf Preset-Registrierungsfehler pruefen:',
  '  journalctl --user -u dsh-native -n 200 --no-pager | grep -i preset',
  '- Der alte roots-Abschnitt in ~/.dsh/settings.yaml (agent-presets) hat in 0.1.7 kein Ziel-Entry',
  '  und wird beim einmaligen Import verworfen (bleibt in settings.yaml.imported).',
  '',
  '- Datei-Inventar in 0.1.7: Agent-Presets sind Plugin-Rows; ein Verzeichnis-Preset wie',
  '  agent-presets/<id>/agent.cordis.yml wird nicht mehr gescannt.',
  '',
].join('\n')
writeFileSync(join(outDir, 'MIGRATION_NOTES.md'), notes)

console.log('Preset          :', presetId, title ? '(' + title + ')' : '')
console.log('Quelle          :', compositionPath)
console.log('Komposition     :', composition.split('\n').length, 'Zeilen')
console.log('Umbenennungen   :', renames.length > 0 ? renames.join('; ') : 'keine')
console.log('Ausgabe         :')
console.log('  ' + join(outDir, 'cordis.patch.candidate.yml'))
console.log('  ' + join(outDir, 'preset-' + presetId + '.composition.yml'))
console.log('  ' + join(outDir, 'MIGRATION_NOTES.md'))
console.log('')
console.log('Hinweis: Es wurde NICHTS in ~/.dsh oder im Checkout veraendert.')
