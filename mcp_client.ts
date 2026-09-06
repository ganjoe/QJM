import { Client } from "npm:@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "npm:@modelcontextprotocol/sdk/client/sse.js";
const key = Deno.env.get("MCP_ACCESS_KEY") || "";
const transport = new SSEClientTransport(new URL(`http://localhost:8790/?key=${key}`));
const client = new Client({ name: "test-client", version: "1.0.0" }, { capabilities: {} });
async function main() {
  await client.connect(transport);
  const tools = await client.listTools();
  console.log(JSON.stringify(tools, null, 2));
}
main().catch(console.error);
