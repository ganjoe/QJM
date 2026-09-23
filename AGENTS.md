# QJM Workspace – Agent Instructions

## Shelly PowerMeter (`openbrain-shelly`) – Pro-Watt-Optimierung & Debugging

Die MCP-Tools `mcp__openbrain-shelly__*` liefern Echtzeit- und Verbrauchsdaten der Server-Steckdose
(Ultron5000, Shelly Plug S Gen3). Nutze sie gezielt, wenn du:

- **Pro Watt optimierst**: herausfinden, welche Workloads wie viel ziehen, Verbrauchsspitzen und
  Kosten (€/kWh) bewerten, "lohnt sich dieser Job?"-Fragen fundiert beantworten.
- **Debuggen willst, ob starke Verbraucher aktiv sind** (GPU/LLM-Inferenz, große Datenjobs):
  `shelly_get_metrics` zeigt Leistung (W), Spannung (V), Strom (A), Temperatur und Relaiszustand
  in Echtzeit. Korreliere das mit `openbrain-cco.get_system_metrics` (GPU-Auslastung/Watt, CPU/RAM),
  um Last und Wandverbrauch zu verbinden. ~0 W = kein relevanter Verbraucher aktiv (Idle).
- **Verbrauchshistorie** brauchst: `shelly_get_consumption_history` (live/24h/7d/30d/1y/all) mit kWh
  und Kosten. Die tägliche Aggregation (min/avg/max für Leistung, Spannung, Strom, Temperatur +
  Strompreis/Kosten) liegt in Supabase in der Tabelle `shelly_daily_stats`.
- **Die Steckdose steuern** willst: `shelly_set_switch` (bewusstes An/Aus – bleibt aus),
  `shelly_power_cycle` (autonomer Hardware-Timer, auch bei totem Server), `shelly_configure_auto_on`
  (5s-Failsafe im Shelly-Flash).

Wichtig: Die Steckdose versorgt den QJM-Server. Ein bewusstes Ausschalten über `shelly_set_switch(false)`
bleibt aus; für einen Reboot den Power-Cycle nutzen. Für ein geordnetes Herunterfahren gibt es
`POST /api/devices/server-plug/system` bzw. `powermeter/system-control.sh`.
