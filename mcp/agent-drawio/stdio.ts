import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { registerDrawioTools } from "./tools/drawio.ts";

const server = new McpServer({
  name: "openbrain-drawio-stdio",
  version: "2.0.0",
});

registerDrawioTools(server);

const transport = new StdioServerTransport();
await server.connect(transport);
