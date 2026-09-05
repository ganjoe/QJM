#!/usr/bin/env python3
"""Automated Validation Suite for QJM Central Feature Registry & Presets.

Verifies the entire pipeline:
1. Supabase/Postgres tables & counts (pca_features, pca_feature_sets, pca_feature_set_members)
2. PCA-Service REST API endpoints (/api/features/registry, /api/presets, /api/presets/{preset})
3. Chart Viewer Orchestrator module (resolve_preset, build_display_stock with offline & online indicators)
4. Chart Viewer HTTP Server API (/api/command DISPLAY_STOCK)
5. MCP Server Tool Definition (agent-pca chart_viewer.ts)

Exit code 0 on success, non-zero on failure.
"""

import sys
import json
import urllib.request
import urllib.error
from pathlib import Path

# Add chart_viewer/src to sys.path so we can import orchestrator
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT / "chart_viewer" / "src"))

PASS = "[\033[92mPASS\033[0m]"
FAIL = "[\033[91mFAIL\033[0m]"
INFO = "[\033[94mINFO\033[0m]"

failures = 0


def record_result(name: str, success: bool, detail: str = ""):
    global failures
    if success:
        print(f"  {PASS} {name} {detail}")
    else:
        failures += 1
        print(f"  {FAIL} {name} {detail}")


def http_get(url: str, timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "QJM-Validator/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def http_post(url: str, payload: dict, timeout: float = 5.0) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "User-Agent": "QJM-Validator/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ==============================================================================
# Phase 1: Database Tables & Schema (PostgreSQL via PCA-Service PostgREST API)
# ==============================================================================
print(f"\n{INFO} Step 1: Validating Database Schema & Data via Supabase...")

try:
    reg = http_get("http://127.0.0.1:8794/api/features/registry")
    preset_list = http_get("http://127.0.0.1:8794/api/presets")
    features_cnt = reg.get("count", 0)
    presets_cnt = preset_list.get("count", 0)

    record_result("pca_features table in Supabase", features_cnt == 17, f"({features_cnt}/17 features)")
    record_result("pca_feature_sets table in Supabase", presets_cnt == 4, f"({presets_cnt}/4 presets)")
except Exception as e:
    record_result("Database connection via Supabase", False, str(e))


# ==============================================================================
# Phase 2: PCA-Service REST API (Port 8794)
# ==============================================================================
print(f"\n{INFO} Step 2: Validating PCA-Service REST API (http://127.0.0.1:8794)...")

# 2.1 Feature Registry
try:
    reg = http_get("http://127.0.0.1:8794/api/features/registry")
    features = reg.get("features", [])
    record_result(
        "GET /api/features/registry",
        len(features) == 17,
        f"(received {len(features)} features, plot_types={reg.get('plot_types')})"
    )
except Exception as e:
    record_result("GET /api/features/registry", False, str(e))

# 2.2 Presets List
try:
    preset_list = http_get("http://127.0.0.1:8794/api/presets")
    presets = preset_list.get("presets", {})
    expected_presets = {"default", "trend_template", "momentum", "clean"}
    has_all = expected_presets.issubset(set(presets.keys()))
    record_result(
        "GET /api/presets",
        has_all,
        f"(presets: {sorted(presets.keys())})"
    )
except Exception as e:
    record_result("GET /api/presets", False, str(e))

# 2.3 Specific Preset: trend_template
try:
    tt = http_get("http://127.0.0.1:8794/api/presets/trend_template")
    inds = tt.get("indicators", [])
    topbar = tt.get("topbar_metrics", [])
    record_result(
        "GET /api/presets/trend_template",
        len(inds) == 6 and len(topbar) == 3,
        f"({len(inds)}/6 indicators, topbar={topbar})"
    )
except Exception as e:
    record_result("GET /api/presets/trend_template", False, str(e))

# 2.4 Specific Preset: momentum (online EMAs)
try:
    mom = http_get("http://127.0.0.1:8794/api/presets/momentum")
    inds = mom.get("indicators", [])
    record_result(
        "GET /api/presets/momentum",
        len(inds) == 2,
        f"({len(inds)}/2 indicators: {[i.get('canonical_id') for i in inds]})"
    )
except Exception as e:
    record_result("GET /api/presets/momentum", False, str(e))


# ==============================================================================
# Phase 3: Chart Viewer Orchestrator Module
# ==============================================================================
print(f"\n{INFO} Step 3: Validating Chart Viewer Orchestrator (Python Module)...")

try:
    from chart_viewer.orchestrator import build_display_stock, resolve_preset

    # 3.1 Resolve preset
    resolved_tt = resolve_preset("trend_template")
    record_result(
        "orchestrator.resolve_preset('trend_template')",
        len(resolved_tt.get("indicators", [])) == 6,
        f"({len(resolved_tt.get('indicators', []))} indicators)"
    )

    # 3.2 Build DISPLAY_STOCK with trend_template (offline SMAs + Topbar)
    ds_tt = build_display_stock("DELL", preset="trend_template")
    tt_bars = len(ds_tt.get("bars", []))
    tt_overlays = len(ds_tt.get("overlays", []))
    tt_topbar = bool(ds_tt.get("topbar"))
    record_result(
        "orchestrator.build_display_stock('DELL', preset='trend_template')",
        tt_bars >= 200 and tt_overlays == 6 and tt_topbar,
        f"(bars={tt_bars}, overlays={tt_overlays}, topbar={tt_topbar})"
    )

    # 3.3 Build DISPLAY_STOCK with momentum (on-the-fly calculated EMAs)
    ds_mom = build_display_stock("DELL", preset="momentum")
    mom_bars = len(ds_mom.get("bars", []))
    mom_overlays = len(ds_mom.get("overlays", []))
    record_result(
        "orchestrator.build_display_stock('DELL', preset='momentum')",
        mom_bars >= 200 and mom_overlays == 2,
        f"(bars={mom_bars}, overlays={mom_overlays} on-the-fly EMAs)"
    )

    # 3.4 Build DISPLAY_STOCK with clean (candles only)
    ds_clean = build_display_stock("DELL", preset="clean")
    clean_bars = len(ds_clean.get("bars", []))
    clean_overlays = len(ds_clean.get("overlays", []))
    record_result(
        "orchestrator.build_display_stock('DELL', preset='clean')",
        clean_bars >= 200 and clean_overlays == 0,
        f"(bars={clean_bars}, overlays={clean_overlays})"
    )

except Exception as e:
    record_result("Chart Viewer Orchestrator execution", False, str(e))


# ==============================================================================
# Phase 4: Chart Viewer HTTP Server API (Port 8766)
# ==============================================================================
print(f"\n{INFO} Step 4: Validating Chart Viewer HTTP Server (http://127.0.0.1:8766)...")

for preset_name, expected_overlays in [("trend_template", 6), ("momentum", 2), ("default", 3), ("clean", 0)]:
    try:
        payload = {"action": "DISPLAY_STOCK", "symbol": "DELL", "preset": preset_name}
        res = http_post("http://127.0.0.1:8766/api/command", payload)
        ok = res.get("status") == "ok" and res.get("overlays") == expected_overlays
        record_result(
            f"POST /api/command (preset='{preset_name}')",
            ok,
            f"(status={res.get('status')}, bars={res.get('bars')}, overlays={res.get('overlays')}/{expected_overlays})"
        )
    except Exception as e:
        record_result(f"POST /api/command (preset='{preset_name}')", False, str(e))


# ==============================================================================
# Phase 5: MCP Tool Definition & Schema (agent-pca chart_viewer.ts)
# ==============================================================================
print(f"\n{INFO} Step 5: Validating PCA MCP Tool Definition (chart_viewer.ts)...")

chart_viewer_ts = WORKSPACE_ROOT / "mcp" / "agent-pca" / "tools" / "chart_viewer.ts"
if chart_viewer_ts.exists():
    content = chart_viewer_ts.read_text()
    has_preset_schema = 'preset: z.enum(["default", "trend_template", "momentum", "clean"])' in content
    has_delegation = 'action: "DISPLAY_STOCK"' in content and "CHART_VIEWER_API_URL" in content
    record_result(
        "chart_viewer.ts preset schema",
        has_preset_schema,
        "(preset parameter registered in inputSchema)"
    )
    record_result(
        "chart_viewer.ts delegation to Chart Server",
        has_delegation,
        "(DISPLAY_STOCK delegates to Chart Viewer Server)"
    )
else:
    record_result("chart_viewer.ts exists", False, "File not found")


# ==============================================================================
# Final Summary
# ==============================================================================
print("\n" + "=" * 60)
if failures == 0:
    print(f" \033[92m✅ ALL VALIDATION CHECKS PASSED!\033[0m")
    print(" Pipeline is completely operational from Supabase DB to Desktop Viewer.")
    print("=" * 60 + "\n")
    sys.exit(0)
else:
    print(f" \033[91m❌ VALIDATION FAILED WITH {failures} ERROR(S)!\033[0m")
    print("=" * 60 + "\n")
    sys.exit(1)
