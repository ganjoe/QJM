import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { supabase, log, getEmbedding, WEB_SCRAPER_URL, SWITCHYARD_URL, AGENT_ID } from "./shared.ts";

export function registerWebTools(server: McpServer) {
  // 1. Tool: web_scrape
  server.registerTool(
    "web_scrape",
    {
      title: "Web Scrape",
      description: "Scrape a web page to extract headlines, article text, or structured content.",
      inputSchema: {
        url: z.string().describe("The URL to scrape"),
        selector: z.string().optional().describe("CSS selector to extract specific elements (e.g. 'h3.headline a'). If omitted, extracts main article content."),
        max_items: z.number().optional().default(20).describe("Maximum number of items to extract (default: 20)"),
      },
    },
    async ({ url, selector, max_items }: any) => {
      try {
        log.info(`[Web] Scraping: ${url}${selector ? ` (selector: ${selector})` : ""}`);

        const res = await fetch(`${WEB_SCRAPER_URL}/scrape`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, selector, max_items: max_items || 20 }),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
          return { content: [{ type: "text", text: `Fehler beim Scrapen: ${err.error}` }], isError: true };
        }

        const data = await res.json();

        if (data.items && data.items.length > 0) {
          const lines = data.items.map((item: any, i: number) => {
            const link = item.href ? ` → ${item.href}` : "";
            return `${i + 1}. ${item.text}${link}`;
          });
          return {
            content: [{
              type: "text",
              text: `**${data.title}** (${data.items.length} Ergebnisse)\nQuelle: ${url}\nZeitpunkt: ${data.scraped_at}\n\n${lines.join("\n")}`
            }]
          };
        }

        if (data.markdown) {
          return {
            content: [{
              type: "text",
              text: `**${data.title}**\nQuelle: ${url}\nZeitpunkt: ${data.scraped_at}\n\n${data.markdown}`
            }]
          };
        }

        return { content: [{ type: "text", text: `Keine Inhalte auf ${url} gefunden.` }] };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Web-Scrape fehlgeschlagen: ${err.message}` }], isError: true };
      }
    }
  );

  // 2. Tool: web_extract_metrics
  server.registerTool(
    "web_extract_metrics",
    {
      title: "Extract Financial Metrics",
      description: "Extract financial metrics (Market Cap, P/E, EPS, Volume, etc.) for a stock ticker from financial websites.",
      inputSchema: {
        ticker: z.string().describe("Stock ticker symbol (e.g. AAPL, MSFT, TSLA)"),
        metrics: z.array(z.string()).optional().describe("Specific metrics to extract. If omitted, extracts all available."),
        url: z.string().optional().describe("Custom financial page URL."),
      },
    },
    async ({ ticker, metrics, url }: any) => {
      try {
        log.info(`[Web] Extracting metrics for ${ticker}${url ? ` from ${url}` : ""}`);

        const res = await fetch(`${WEB_SCRAPER_URL}/extract-metrics`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ticker, metrics, url }),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
          return { content: [{ type: "text", text: `Fehler bei Metrik-Extraktion: ${err.error}` }], isError: true };
        }

        const data = await res.json();
        const metricsEntries = Object.entries(data.metrics || {});

        if (metricsEntries.length === 0) {
          return { content: [{ type: "text", text: `Keine Metriken für ${ticker} gefunden.` }] };
        }

        const lines = metricsEntries.map(([k, v]) => `  ${k}: ${v}`);
        return {
          content: [{
            type: "text",
            text: `**${data.ticker}** (Quelle: ${data.source})\nURL: ${data.url}\nZeitpunkt: ${data.scraped_at}\n\n${lines.join("\n")}`
          }]
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Metrik-Extraktion fehlgeschlagen: ${err.message}` }], isError: true };
      }
    }
  );

  // 3. Tool: web_download_report
  server.registerTool(
    "web_download_report",
    {
      title: "Download Report",
      description: "Download a PDF report from a URL or find PDF links on an investor relations page.",
      inputSchema: {
        url: z.string().describe("Direct PDF URL or investor relations page URL"),
        company: z.string().describe("Company name"),
        report_type: z.enum(["annual_report", "quarterly", "earnings", "10k", "10q", "8k", "other"]).optional().default("other").describe("Type of report"),
        find_pdfs_only: z.boolean().optional().default(false).describe("If true, only lists PDF links without downloading"),
      },
    },
    async ({ url, company, report_type, find_pdfs_only }: any) => {
      try {
        if (find_pdfs_only) {
          const res = await fetch(`${WEB_SCRAPER_URL}/find-pdfs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
          });

          if (!res.ok) {
            const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
            return { content: [{ type: "text", text: `PDF-Suche fehlgeschlagen: ${err.error}` }], isError: true };
          }

          const data = await res.json();
          if (!data.pdf_links || data.pdf_links.length === 0) {
            return { content: [{ type: "text", text: `Keine PDFs auf ${url} gefunden.` }] };
          }

          const lines = data.pdf_links.map((l: any, i: number) => `${i + 1}. [${l.text}](${l.href})`);
          return {
            content: [{
              type: "text",
              text: `**PDF-Links auf ${url}:**\n\n${lines.join("\n")}`
            }]
          };
        }

        const res = await fetch(`${WEB_SCRAPER_URL}/download`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, company, report_type }),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
          return { content: [{ type: "text", text: `Download fehlgeschlagen: ${err.error}` }], isError: true };
        }

        const data = await res.json();
        const embedding = await getEmbedding(`${company} ${report_type} report ${data.text_preview?.slice(0, 200) || ""}`);

        await supabase.from("agent_workspace").insert({
          agent_id: AGENT_ID,
          artifact_type: "web_report",
          title: `${company} ${report_type} Report`,
          content: data.text_preview || "(kein Text extrahiert)",
          status: "embedded",
          metadata: {
            company,
            report_type,
            source_url: url,
            file_path: data.file_path,
            file_name: data.file_name,
            size_bytes: data.size_bytes,
            pages: data.pages,
          },
          embedding,
        });

        return {
          content: [{
            type: "text",
            text: `**Report heruntergeladen:**\n- Firma: ${company}\n- Typ: ${report_type}\n- Datei: ${data.file_name}\n- Größe: ${(data.size_bytes / 1024 / 1024).toFixed(2)} MB\n- Seiten: ${data.pages || "?"}\n\n**Text-Vorschau:**\n${data.text_preview?.slice(0, 500) || "(kein Text)"}`
          }]
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: `Report-Download fehlgeschlagen: ${err.message}` }], isError: true };
      }
    }
  );

  // 4. Tool: web_ocr_extract
  server.registerTool(
    "web_ocr_extract",
    {
      title: "OCR Extract (Vision)",
      description: "Extract text or data from a web page screenshot using a Vision model.",
      inputSchema: {
        url: z.string().describe("URL to screenshot and analyze"),
        prompt: z.string().optional().default("Extrahiere alle sichtbaren Zahlen, Kennzahlen und Texte aus diesem Screenshot.").describe("Instruction for vision model"),
        full_page: z.boolean().optional().default(false).describe("If true, captures full page"),
      },
    },
    async ({ url, prompt, full_page }: any) => {
      try {
        const screenshotRes = await fetch(`${WEB_SCRAPER_URL}/screenshot`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, full_page: full_page || false }),
        });

        if (!screenshotRes.ok) {
          const err = await screenshotRes.json().catch(() => ({ error: `HTTP ${screenshotRes.status}` }));
          return { content: [{ type: "text", text: `Screenshot fehlgeschlagen: ${err.error}` }], isError: true };
        }

        const screenshot = await screenshotRes.json();

        const visionPayload = {
          model: "local",
          messages: [
            {
              role: "user",
              content: [
                { type: "text", text: prompt || "Beschreibe was du siehst und extrahiere alle Zahlen und Texte." },
                {
                  type: "image_url",
                  image_url: { url: `data:image/png;base64,${screenshot.base64}` },
                },
              ],
            },
          ],
          temperature: 0.1,
          max_tokens: 4096,
        };

        const swBaseUrl = SWITCHYARD_URL.endsWith("/v1") ? SWITCHYARD_URL : `${SWITCHYARD_URL}/v1`;
        const llmRes = await fetch(`${swBaseUrl}/chat/completions`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": "Bearer switchyard",
          },
          body: JSON.stringify(visionPayload),
        });

        if (!llmRes.ok) {
          const errText = await llmRes.text();
          return { content: [{ type: "text", text: `Vision-Modell-Fehler (${llmRes.status}): ${errText}` }], isError: true };
        }

        const llmData: any = await llmRes.json();
        const extractedText = llmData.choices?.[0]?.message?.content || "(keine Antwort vom Vision-Modell)";

        return {
          content: [{
            type: "text",
            text: `**OCR-Ergebnis für ${url}:**\n\n${extractedText}`
          }]
        };
      } catch (err: any) {
        return { content: [{ type: "text", text: `OCR-Extraktion fehlgeschlagen: ${err.message}` }], isError: true };
      }
    }
  );
}
