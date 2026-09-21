import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { generateDrawioXml, DiagramNode, DiagramEdge } from "./drawio_generator.ts";
import { BASE, slugify, writeDiagram, listDiagrams, readDiagram } from "./storage.ts";

export function registerDrawioTools(server: McpServer) {
  server.tool(
    "create_drawio_diagram",
    "Erzeugt ein draw.io-Diagramm (Architektur, Flowchart, Sequenz) und speichert es als .drawio-Datei lokal im dsh_playground des QJM-Workspace. Kein Google Drive, keine Cloud. Der Dateiname wird aus dem Titel abgeleitet (slugifiziert, Endung .drawio). Nenne dem Nutzer nach dem Aufruf den vollstaendigen Pfad.",
    {
      title: z.string().describe("Titel des Diagramms; Grundlage fuer den Dateinamen"),
      nodes: z.array(z.object({
        id: z.string().describe("Eindeutige Knoten-ID, z.B. node_1"),
        label: z.string().describe("Text im Knoten"),
        shape: z.enum(["rounded", "rectangle", "ellipse", "rhombus", "cylinder"]).optional().describe("Form des Knotens"),
        fillColor: z.string().optional().describe("HEX-Fuellfarbe, z.B. #dae8fc"),
        strokeColor: z.string().optional().describe("HEX-Randfarbe, z.B. #6c8ebf"),
        fontColor: z.string().optional().describe("HEX-Schriftfarbe"),
        x: z.number().optional(),
        y: z.number().optional(),
        width: z.number().optional(),
        height: z.number().optional(),
      })).describe("Knoten des Diagramms"),
      edges: z.array(z.object({
        id: z.string().optional(),
        source: z.string().describe("Quell-Knoten-ID"),
        target: z.string().describe("Ziel-Knoten-ID"),
        label: z.string().optional().describe("Beschriftung der Verbindung"),
        style: z.string().optional().describe("Optionaler draw.io-Style"),
      })).optional().describe("Verbindungspfeile"),
      filename: z.string().optional().describe("Optionaler Dateiname; Default aus title"),
      overwrite: z.boolean().optional().describe("Bestehende Datei ersetzen (Default false)"),
      return_xml: z.boolean().optional().describe("XML in der Antwort mitgeben (Default false)"),
    },
    async ({ title, nodes, edges, filename, overwrite, return_xml }) => {
      try {
        const name = filename && filename.trim().length > 0 ? filename : slugify(title);
        const xml = generateDrawioXml(title, nodes as DiagramNode[], (edges || []) as DiagramEdge[]);
        const abs = writeDiagram(name, xml, overwrite === true);
        const parts = abs.split("/");
        const fileName = parts[parts.length - 1];
        let text = "Diagramm erstellt.\n" +
          "Pfad: " + abs + "\n" +
          "Datei: " + fileName + "\n" +
          "Oeffnen: Datei in draw.io laden (app.diagrams.net -> Oeffnen -> Datei waehlen) oder im lokalen Editor.\n" +
          "Ablage: " + BASE;
        if (return_xml === true) {
          text += "\n\nXML:\n" + xml;
        }
        return { content: [{ type: "text", text: text }] };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return { isError: true, content: [{ type: "text", text: "Fehler beim Erstellen des Diagramms: " + message }] };
      }
    },
  );

  server.tool(
    "list_drawio_diagrams",
    "Listet alle lokalen .drawio-Dateien im dsh_playground des QJM-Workspace mit Name, Pfad, Groesse und Aenderungszeit.",
    {},
    async () => {
      try {
        const files = listDiagrams();
        return {
          content: [{
            type: "text",
            text: JSON.stringify({ base: BASE, count: files.length, files: files }, null, 2),
          }],
        };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return { isError: true, content: [{ type: "text", text: "Fehler beim Auflisten: " + message }] };
      }
    },
  );

  server.tool(
    "read_drawio_diagram",
    "Liest eine lokale .drawio-Datei aus dem dsh_playground des QJM-Workspace ueber ihren Dateinamen (oder einen Pfad relativ zum Diagramm-Ordner) und gibt das XML zurueck.",
    {
      filename: z.string().describe("Dateiname der .drawio-Datei, z.B. architektur.drawio"),
    },
    async ({ filename }) => {
      try {
        const xml = readDiagram(filename);
        return { content: [{ type: "text", text: xml }] };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return { isError: true, content: [{ type: "text", text: "Fehler beim Lesen: " + message }] };
      }
    },
  );
}
