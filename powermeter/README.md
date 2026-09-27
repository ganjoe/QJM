# powermeter – reserviert für den PowerMeter-MCP-Server

> **Umzug am 2026-09-23:** Der PowerMeter-Dienst (FastAPI + Poller + Parquet-Storage + Web-Dashboard)
> liegt jetzt vollständig unter `/home/daniel/powermeter`. Die systemd-Unit wurde entsprechend
> umgestellt: `WorkingDirectory=/home/daniel/powermeter`, `ExecStart=/home/daniel/powermeter/run.sh`.
> Dieses Verzeichnis ist damit **frei für den MCP-Server für den PowerMeter**.

## Aktueller Stand
- **Dienst:** `/home/daniel/powermeter` (venv: `/home/daniel/powermeter/.venv`), API auf `http://127.0.0.1:8800`
- **MCP-Server (openbrain-shelly):** `/home/daniel/QJM/mcp/agent-shelly`
  (registriert in `mcp/antigravity_mcp_config.json`, spricht `POWERMETER_API_URL=http://127.0.0.1:8800`)
- **Backup vor dem Umzug:** `/home/daniel/QJM/backups/powermeter-move-*.tgz`

## Mögliche nächste Schritte
- `mcp/agent-shelly` hierher verschieben und den Pfad in `mcp/antigravity_mcp_config.json` anpassen.
- Oder hier einen eigenen, schlanken MCP-Server für den PowerMeter neu aufsetzen.
