import { registerChartViewerTools } from "./mcp/agent-pca/tools/chart_viewer.ts";
import { McpServer } from "npm:@modelcontextprotocol/sdk/server/mcp.js";
const server = new McpServer({ name: "openbrain-pca", version: "2.0.0" });
registerChartViewerTools(server);
console.log("Success");
