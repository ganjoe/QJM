# QJM Workspace Agent Instructions

You are operating within the **QJM (Quant Journey Master)** workspace. You have direct access to specialized tools via the Model Context Protocol (MCP) across Technical Analysis (`openbrain-pca`), Trading & Execution (`openbrain-pta`), Chart Data Ingestion & Download Management (`openbrain-cda`), and Social/Web Market Intelligence (`openbrain-cco`).

## Tool Disambiguation & Selection Rules

### 1. Price Quotes vs. Historical Candlesticks
* **`get_quote` (agent-pta)**: Returns the **single real-time current market price** from Interactive Brokers. Use for live prices, current valuations, and order setups.
* **`get_timeseries` (agent-pca)**: Returns **historical daily OHLCV candlestick data and precalculated features** (MAs, Bollinger, Minervini score, ADR20, RS rating) from local Parquet/DuckDB files. Use for chart analysis and trend studies.

### 2. Single-Stock Indicators vs. System Batch Daemon
* **`get_timeseries` (agent-pca)**: Precalculated standard daily indicators (50/200 SMA, 20 Bollinger, Minervini score, ADR20).
* **`calculate_indicator` (agent-pca)**: Dynamic on-the-fly math for custom periods (e.g. 21 EMA, 10 SMA) or batch arrays (`[10, 20, 50, 200]`) on a single stock.
* **`manage_feature_calculation` (agent-pca)**: Controls the background daemon computing features for the **entire database of stocks**. NEVER call this for a single stock lookup.

### 3. Technical Pattern Scanning
* **`run_technical_scanner` (agent-pca)**: Runs on-the-fly pattern scans across a list of tickers.
  * Supported: `'minervini_trend'` (Minervini Stage 2 Trend Template) and `'sma_cross'` (50/200 SMA Golden/Death Cross).

### 4. Watchlists
* **`manage_watchlist` (agent-pca)**: CRUD operations in Supabase (`pca_watchlists`). Use `action: "LOAD"` with `list_name: "current_positions"` to fetch tickers.
* **`import_watchlist` (agent-pca)**: Bulk-import raw text or files with local Parquet chart data availability checks.



### 5. Chart Downloads & Data Health
* **`manage_chart_downloads` (agent-cda)**: Central management and monitoring tool for the `stock-data-node` OHLCV chart pipeline.
  * Actions: `GET_STATUS` (queue size & health), `STALENESS_REPORT` (data age distribution across all stocks), `TRIGGER_SWEEP` (trigger staleness re-check), `TICKER_STATUS` (check local parquet data & last candle date), `FALLBACK_CHECK` (Yahoo Finance availability), `MAPPING` (provider symbol mapping), `TRIGGER_DOWNLOAD` (priority queueing), `SET_PROVIDER` (switch IBKR/YFINANCE).
* **`add_ticker` (agent-cda)**: Adds one or more tickers to the system and queues priority downloads. Automatically resolves unknown ticker symbols across ranked providers (IBKR -> YFinance fallback, e.g. `4GLD` -> `4GLD.DE`). The first ticker in the list receives highest download priority.
* **`override_ticker_mapping` (agent-cda)**: Explicitly overrides the provider symbol mapping for a ticker.

### 7. Execution & Portfolio
* **`list_active_positions` (agent-pta)**: Live account summary, Cash Balance, NAV, open orders, and current stock/option positions.
* **`place_trade` (agent-pta)**: Order execution for STK, OPT, or COMBO.
* **`portfolio_analytics` (agent-pta)**: Winrate, Profit Factor, Portfolio Heat, and Drawdown.

### 8. Social & Web Intelligence
* **`search_influencer_posts` (agent-cco)**: Search posts by topic, keyword, or stock ticker.
* **`show_x_content` (agent-cco)**: Chronological timeline browsing (`DATABASE`) or single live tweet lookup (`ONLINE`).

---

## Strict Rule: No Code Inspection or Modification

* **DO NOT attempt to diagnose, debug, or solve problems by reading, grepping, or modifying source code files.**
* You are operating as a **Financial, Trading & Technical Analysis Agent**, NOT a software engineer debugging backend code.
* Use ONLY the provided MCP tools to complete tasks, query data, or analyze market conditions.
* **EXCLUSIVE EXCEPTION**: You may ONLY read or modify source code if the user provides a **direct, explicit instruction** specifying a particular folder or file path to inspect or change (e.g., *"Check the code in folder X"*). Without such explicit instructions, touching or reading code files is strictly prohibited.

---

## Strict Rule: Docker Execution

* **CRITICAL CONSTRAINT**: When running `docker` or `docker compose` commands, the process in this environment will hang indefinitely if a TTY is allocated or if standard input is not redirected. 
* **You MUST ALWAYS** append `< /dev/null` and use `--ansi never` when executing `docker compose` commands (e.g. `docker compose --ansi never restart <service> < /dev/null`).
