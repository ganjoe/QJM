import { join, resolve } from "node:path";

// Speicherwurzel fuer alle .drawio-Dateien. Im Container zeigt der Pfad auf
// den gemounteten dsh_playground; lokal kann DRAWIO_BASE_DIR ueberschrieben werden.
export const BASE = resolve(Deno.env.get("DRAWIO_BASE_DIR") ?? "/dsh_playground/drawio");

export function ensureBase(): void {
  Deno.mkdirSync(BASE, { recursive: true });
}

// Leitet aus einem Titel einen sicheren Dateinamen ab: lowercase, nur
// a-z 0-9 . _ -, Endung .drawio.
export function slugify(title: string): string {
  const cleaned = title
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 120);
  const name = cleaned.length > 0 ? cleaned : "diagram";
  return name.endsWith(".drawio") ? name : name + ".drawio";
}

// Loest einen Namen relativ zur Basis auf und verhindert jeden Ausbruch
// (Path Traversal, absolute Pfade).
export function resolveInBase(name: string): string {
  if (!name || name.trim().length === 0) {
    throw new Error("Dateiname fehlt");
  }
  const abs = resolve(BASE, name);
  if (abs === BASE || !abs.startsWith(BASE + "/")) {
    throw new Error("Pfad ausserhalb des Diagramm-Ordners: " + name);
  }
  return abs;
}

export interface DiagramInfo {
  name: string;
  path: string;
  bytes: number;
  modified: string;
}

function fileExists(abs: string): boolean {
  try {
    return Deno.statSync(abs).isFile;
  } catch (_e) {
    return false;
  }
}

// Schreibt atomar: erst Temp-Datei, dann Rename. Kein halber Zustand.
export function writeDiagram(name: string, xml: string, overwrite: boolean): string {
  ensureBase();
  const abs = resolveInBase(name);
  if (fileExists(abs) && !overwrite) {
    throw new Error("Datei existiert bereits: " + name + " (overwrite=true zum Ersetzen)");
  }
  const tmp = abs + ".tmp-" + crypto.randomUUID();
  Deno.writeTextFileSync(tmp, xml);
  Deno.renameSync(tmp, abs);
  return abs;
}

export function listDiagrams(): DiagramInfo[] {
  ensureBase();
  const out: DiagramInfo[] = [];
  for (const entry of Deno.readDirSync(BASE)) {
    if (!entry.isFile || !entry.name.endsWith(".drawio")) continue;
    const abs = join(BASE, entry.name);
    const st = Deno.statSync(abs);
    out.push({
      name: entry.name,
      path: abs,
      bytes: st.size,
      modified: st.mtime ? st.mtime.toISOString() : "",
    });
  }
  out.sort((a, b) => (a.modified < b.modified ? 1 : -1));
  return out;
}

export function readDiagram(name: string): string {
  ensureBase();
  const abs = resolveInBase(name);
  if (!Deno.statSync(abs).isFile) {
    throw new Error("Keine Datei: " + name);
  }
  return Deno.readTextFileSync(abs);
}
