// @ts-nocheck
declare const Deno: any;
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { registerShellyTools } from "./tools/shelly.ts";

const server = new McpServer({
  name: "openbrain-shelly",
  version: "2.0.0",
});

registerShellyTools(server);

const transport = new StdioServerTransport();
await server.connect(transport);
