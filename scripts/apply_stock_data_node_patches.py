#!/usr/bin/env python3
"""
Patch script to integrate metadata_enricher into stock-data-node.
Updates:
1. stock-data-node/src/api_server.py (adds /metadata/enrich endpoint and auto-enrich in /add)
2. stock-data-node/src/providers/universe_sync.py (calls reconcile on sync)
3. stock-data-node/src/main.py (starts metadata_reconciler_loop)
"""
from pathlib import Path
import sys

BASE_DIR = Path("/home/daniel/stock-data-node/src")

# 1. Update api_server.py
api_server_file = BASE_DIR / "api_server.py"
content = api_server_file.read_text(encoding="utf-8")

# Check if already patched
if "/metadata/enrich" not in content:
    target = '        return {"results": results}\n'
    replacement = '''        # Trigger immediate metadata enrichment for successfully added/resolved tickers
        successful_tickers = [r["ticker"] for r in results if r.get("status") in ["ok", "resolved"]]
        if successful_tickers:
            try:
                from metadata_enricher import enrich_tickers_batch
                asyncio.create_task(asyncio.to_thread(enrich_tickers_batch, successful_tickers))
            except Exception as enrich_err:
                logger.error("Failed to trigger metadata enrichment for %s: %s", successful_tickers, enrich_err)

        return {"results": results}
'''
    if target in content:
        content = content.replace(target, replacement, 1)
        print("Patched /add in api_server.py")
    else:
        print("Warning: Could not find target in api_server.py for /add")

    # Add /metadata/enrich endpoint right before @app.post("/data/status-batch")
    target_endpoint = '    @app.post("/data/status-batch")'
    endpoint_code = '''    @app.post("/metadata/enrich")
    async def enrich_metadata_endpoint(req: BatchTickersRequest) -> dict:
        """Enriches metadata for the requested tickers and updates cda_master_universe."""
        from metadata_enricher import enrich_tickers_batch
        res = await asyncio.to_thread(enrich_tickers_batch, req.tickers)
        return {"status": "ok", "enriched_count": len(res), "results": res}

    @app.post("/data/status-batch")'''

    if target_endpoint in content:
        content = content.replace(target_endpoint, endpoint_code, 1)
        print("Added /metadata/enrich endpoint in api_server.py")
    else:
        print("Warning: Could not find target_endpoint in api_server.py")

    api_server_file.write_text(content, encoding="utf-8")
else:
    print("api_server.py already patched.")


# 2. Update universe_sync.py
sync_file = BASE_DIR / "providers" / "universe_sync.py"
sync_content = sync_file.read_text(encoding="utf-8")

if "metadata_enricher" not in sync_content:
    target_sync = '                logger.info("Successfully synced %d tickers to Supabase.", len(tickers))\n'
    replacement_sync = '''                logger.info("Successfully synced %d tickers to Supabase.", len(tickers))
                try:
                    from metadata_enricher import reconcile_missing_metadata
                    reconcile_missing_metadata(supabase_url=sb_url, supabase_key=sb_key)
                except Exception as enrich_err:
                    logger.error("Error triggering metadata reconcile during universe sync: %s", enrich_err)
'''
    if target_sync in sync_content:
        sync_content = sync_content.replace(target_sync, replacement_sync, 1)
        sync_file.write_text(sync_content, encoding="utf-8")
        print("Patched universe_sync.py")
    else:
        print("Warning: Could not find target in universe_sync.py")
else:
    print("universe_sync.py already patched.")


# 3. Update main.py
main_file = BASE_DIR / "main.py"
main_content = main_file.read_text(encoding="utf-8")

if "metadata_reconciler_loop" not in main_content:
    target_main_loop = '    async def shutdown_watcher() -> None:'
    reconciler_code = '''    async def metadata_reconciler_loop() -> None:
        from metadata_enricher import reconcile_missing_metadata, get_config_val
        interval = get_config_val("METADATA_RECONCILE_INTERVAL_SEC", 900.0, float)
        logger.info("ℹ️  Metadata reconciler background task started (interval: %.0fs)", interval)

        # Initial sweep on startup (run in thread to not block event loop)
        try:
            await asyncio.to_thread(reconcile_missing_metadata)
        except Exception as e:
            logger.error("❌ Startup metadata reconcile error: %s", e)

        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

            if shutdown_event.is_set():
                break

            try:
                await asyncio.to_thread(reconcile_missing_metadata)
            except Exception as e:
                logger.error("❌ Periodic metadata reconcile error: %s", e)

    async def shutdown_watcher() -> None:'''

    if target_main_loop in main_content:
        main_content = main_content.replace(target_main_loop, reconciler_code, 1)
        print("Added metadata_reconciler_loop to main.py")
    else:
        print("Warning: Could not find target_main_loop in main.py")

    # Add to gather
    target_gather = '        staleness_sweep_loop(),\n'
    replacement_gather = '        staleness_sweep_loop(),\n        metadata_reconciler_loop(),\n'
    if target_gather in main_content:
        main_content = main_content.replace(target_gather, replacement_gather, 1)
        print("Added metadata_reconciler_loop to asyncio.gather in main.py")
    else:
        print("Warning: Could not find target_gather in main.py")

    main_file.write_text(main_content, encoding="utf-8")
else:
    print("main.py already patched.")

print("All patches processed successfully.")
