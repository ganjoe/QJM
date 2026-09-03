/**
 * trader-mcp-filter
 *
 * Agent-plane row for the `trader` preset. The deployment registers the
 * OpenBrain MCP servers (cco/pta/pca) as GLOBAL tools for every agent, and
 * production dsh exposes no per-agent MCP selector. This plugin narrows the
 * inherited global surface for the preset's scope to exactly the tool names
 * listed in the row's `config.allow` — everything else that is global
 * (any current or future host-level tool) stays masked, while tools this
 * preset registers itself (bash, fs, web, skills, …) remain visible because
 * restrictions never filter a scope's own layer.
 *
 * Defensive by construction: `tools.restrict()` throws on names that are not
 * currently registered as inherited/global tools (e.g. an MCP server still
 * connecting at mount time). We therefore intersect the allowlist with the
 * names the registry currently knows, and skip entirely when nothing known
 * remains, so a temporarily slow server degrades the mask instead of failing
 * the whole preset mount.
 */

const name = 'trader-mcp-filter'
const inject = ['tools']

function apply(ctx, config) {
  const allow = Array.isArray(config && config.allow) ? config.allow : []
  if (allow.length === 0) return

  let known = new Set()
  try {
    known = new Set((ctx.tools.schemas() ?? []).map((tool) => tool.name))
  } catch (error) {
    console.error('[trader-mcp-filter] could not read tool registry:', error && error.message)
  }

  const effective = allow.filter((toolName) => known.has(toolName))
  if (effective.length === 0) {
    console.warn('[trader-mcp-filter] none of the allowlisted MCP tools are registered yet; skipping restriction')
    return
  }

  ctx.effect(() => ctx.tools.restrict({ allow: effective }), 'trader-mcp-filter.restrict()')
}

export { name, inject, apply }