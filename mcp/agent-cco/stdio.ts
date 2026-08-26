import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { registerPipelineTools } from "./tools/pipeline_tools.ts";
import { registerXTools } from "./tools/x_tools.ts";
import { registerYouTubeTools } from "./tools/youtube_tools.ts";
import { registerWebTools } from "./tools/web_tools.ts";
import { registerOpenBrainTools } from "./tools/openbrain_tools.ts";

const server = new McpServer({
  name: "openbrain-cco-stdio",
  version: "2.0.0",
});

registerPipelineTools(server);
registerXTools(server);
registerYouTubeTools(server);
registerWebTools(server);
registerOpenBrainTools(server);

const transport = new StdioServerTransport();
await server.connect(transport);
