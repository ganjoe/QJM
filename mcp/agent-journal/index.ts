// ============================================================================
// mcp/agent-journal/index.ts
//
// MCP-Server openbrain-journal (Trader-Tagebuch), Port 8801.
// Muster: mcp/agent-wiq/index.ts (MCP-SDK 1.24.3, Hono, StreamableHTTP, x-brain-key).
// ============================================================================
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPTransport } from '@hono/mcp';
import { Hono } from 'hono';
import { AGENT_ID, MCP_ACCESS_KEY, log } from './tools/llm.ts';
import { registerJournalTools } from './tools/journal_tools.ts';

const server = new McpServer({
  name: 'openbrain-journal',
  version: '1.0.0',
});

registerJournalTools(server);

const app = new Hono();

app.get('/health', (c) => {
  return c.json({ status: 'healthy', server: 'openbrain-journal', version: '1.0.0', agent: AGENT_ID });
});

// MCP-Endpunkt mit Key-Schutz
app.all('*', async (c) => {
  const provided = c.req.header('x-brain-key') || new URL(c.req.url).searchParams.get('key');
  if (!provided || (MCP_ACCESS_KEY && provided !== MCP_ACCESS_KEY)) {
    return c.json({ error: 'Invalid key' }, 401);
  }
  const transport = new StreamableHTTPTransport();
  await server.connect(transport);
  return transport.handleRequest(c);
});

const port = parseInt(Deno.env.get('PORT') || '8801');
log.info(AGENT_ID.toUpperCase() + ' MCP server starting on port ' + port + '...');
Deno.serve({ port }, app.fetch);
