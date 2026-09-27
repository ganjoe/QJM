"""HTTP-/MCP-Kommando-Dispatch des Chart-Agents.

Aus run_server.ControlHandler.do_POST extrahiert (verhaltensgleich): der
Handler liest nur noch den Request-Body und ruft handle_command() auf.
Dadurch ist der Dispatch ohne Socket testbar (siehe
chart_viewer/tests/test_command_dispatch.py) und wird von der
Lifecycle-Bruecke (ControlService.execute) mitbenutzt.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict

logger = logging.getLogger("chart_viewer.command_api")

# Aktionen, die ueber die ControlService-Fassade laufen: eine Implementierung
# fuer Control Panel (control.request) und MCP (/api/command). Erweitert in
# Phase 5 (LIST_TOPBAR_METRICS) und Phase 6 (SETUP_ASSIGN).
_CONTROL_BRIDGE_ACTIONS = frozenset(
    {
        "GET_CHART_STATE",
        "LIST_WINDOWS",
        "SAVE_CHART",
        "APPLY_CHART",
        "DIFF_CHART",
        "LIST_TOPBAR_METRICS",
    }
)


def handle_command(cmd: Dict[str, Any], agent: Any, server_transport: Any) -> Dict[str, Any]:
    """Eine /api/command-Anweisung ausfuehren und das Antwort-Dict liefern."""
    action = cmd.get("action", "").upper()
    win_id = cmd.get("window_id")

    if action == "OPEN_WINDOW":
        symbol = cmd.get("symbol", "CHART")
        tf = cmd.get("timeframe", {})
        tf_unit = tf.get("unit", "D") if isinstance(tf, dict) else "D"
        tf_mult = tf.get("multiplier", 1) if isinstance(tf, dict) else 1
        sync_group = cmd.get("sync_group_id", "default")
        win_id = win_id or f"win_{symbol.lower()}_{tf_mult}{tf_unit.lower()}"

        agent.open_window(
            window_id=win_id,
            symbol=symbol,
            timeframe_unit=tf_unit,
            multiplier=tf_mult,
            sync_group_id=sync_group,
            position=cmd.get("position"),
            size=cmd.get("size"),
        )
        if "bars" in cmd:
            snap = {
                "symbol": symbol,
                "timeframe": {"unit": tf_unit, "multiplier": tf_mult},
                "sync_group_id": sync_group,
                "bars": cmd["bars"],
                "overlays": cmd.get("overlays", []),
                "annotations": cmd.get("annotations", []),
            }
            agent.send_snapshot(win_id, snap)

        result = {"status": "ok", "action": action, "window_id": win_id}

    elif action == "OPEN_WATCHLIST":
        list_id = cmd.get("list_id")
        if not list_id:
            result = {"error": "Missing list_id"}
        else:
            agent.open_watchlist(
                list_id=list_id,
                display_name=cmd.get("display_name", list_id),
                columns=cmd.get("columns", []),
                rows=cmd.get("rows", []),
                color_flag=cmd.get("color_flag", 0),
                sort_column=cmd.get("sort_column"),
                sort_ascending=cmd.get("sort_ascending", True),
                position=cmd.get("position"),
                size=cmd.get("size"),
            )
            result = {"status": "ok", "action": action, "list_id": list_id}

    elif action == "UPDATE_DATA":
        list_id = cmd.get("list_id")
        if not list_id:
            result = {"error": "Missing list_id"}
        else:
            agent.update_watchlist_data(
                list_id=list_id,
                columns=cmd.get("columns"),
                rows=cmd.get("rows"),
                replace=cmd.get("replace", False),
            )
            result = {"status": "ok", "action": action, "list_id": list_id}

    elif action in ("LOAD_CHART", "SET_SNAPSHOT") and win_id:
        snapshot = cmd.get("snapshot", cmd)
        agent.send_snapshot(win_id, snapshot)
        result = {"status": "ok", "action": action, "window_id": win_id}

    elif action == "ADD_ANNOTATION" and win_id:
        ann = cmd.get("annotation")
        if ann:
            from chart_viewer.models.envelope import make_envelope, MessageKind
            env = make_envelope(
                msg_type="annotation.set",
                payload={"annotation": ann},
                kind=MessageKind.COMMAND,
                window_id=win_id,
            )
            server_transport.send_command(env)
            # Cache in agent series_data
            if win_id in agent.series_data:
                ann_list = agent.series_data[win_id].setdefault("annotations", [])
                ann_list.append(ann)
            result = {"status": "ok", "action": action, "window_id": win_id, "annotation_id": ann.get("id")}
        else:
            result = {"error": "Missing annotation payload"}

    elif action == "REMOVE_ANNOTATION" and win_id:
        ann_id = cmd.get("annotation_id")
        from chart_viewer.models.envelope import make_envelope, MessageKind
        env = make_envelope(
            msg_type="annotation.remove",
            payload={"id": ann_id},
            kind=MessageKind.COMMAND,
            window_id=win_id,
        )
        server_transport.send_command(env)
        if win_id in agent.series_data:
            ann_list = agent.series_data[win_id].get("annotations", [])
            agent.series_data[win_id]["annotations"] = [a for a in ann_list if a.get("id") != ann_id]
        result = {"status": "ok", "action": action, "window_id": win_id, "annotation_id": ann_id}

    elif action == "SET_TOPBAR" and win_id:
        block = {
            "block_id": cmd.get("block_id", "status_block"),
            "position": cmd.get("position", {"row": 0, "col": 0}),
            "content": cmd.get("content", ""),
            "ttl_ms": cmd.get("ttl_ms"),
        }
        from chart_viewer.models.envelope import make_envelope, MessageKind
        env = make_envelope(
            msg_type="topbar.set_block",
            payload=block,
            kind=MessageKind.COMMAND,
            window_id=win_id,
        )
        server_transport.send_command(env)
        # Persist so the block survives a viewer reconnect / layout.restore.
        # Fensterlokale Bloecke liegen neben dem Chart (Ledger), nicht in der
        # Chart-Definition: ein Re-Render loescht sie deshalb nicht.
        if win_id in agent.series_data or win_id in agent.layout_ledger:
            blocks = agent.series_data.setdefault(win_id, {}).setdefault("topbar_blocks", [])
            blocks[:] = [b for b in blocks if b.get("block_id") != block["block_id"]]
            blocks.append(block)
        result = {"status": "ok", "action": action, "window_id": win_id}

    elif action == "CLOSE_WINDOW" and win_id:
        agent.layout_ledger.pop(win_id, None)
        from chart_viewer.models.envelope import make_envelope, MessageKind
        env = make_envelope(
            msg_type="window.command",
            payload={"command": "close"},
            kind=MessageKind.COMMAND,
            window_id=win_id,
        )
        server_transport.send_command(env)
        result = {"status": "ok", "action": action, "window_id": win_id}

    elif action in ("DISPLAY_STOCK", "COMPOSE_CHART"):
        # COMPOSE_CHART ist DISPLAY_STOCK mit einer Ad-hoc-Pane-Liste
        # (Builder-Draft): das Symbol kommt dann aus dem Ledger.
        if action == "COMPOSE_CHART" and not cmd.get("symbol"):
            ledger_entry = agent.layout_ledger.get(cmd.get("window_id") or "", {}) or {}
            cmd["symbol"] = ledger_entry.get("symbol")
        # Save the command state so it can be restored on connect/restart
        try:
            with open("/tmp/last_chart_state.json", "w") as f:
                json.dump(cmd, f)
        except Exception as e:
            logger.warning(f"Failed to save last_chart_state: {e}")

        # Full orchestration: fetch data from PCA-Service, build overlays, push to viewer
        from chart_viewer.orchestrator import build_display_stock, DEFAULT_CHART_LIMIT
        symbol = cmd.get("symbol")
        if not symbol:
            result = {"error": "Missing 'symbol' parameter"}
        else:
            try:
                display_cmd = build_display_stock(
                    symbol=symbol,
                    indicators=cmd.get("indicators"),
                    preset=cmd.get("preset"),
                    chart=cmd.get("chart"),
                    panes=cmd.get("panes"),
                    chart_meta=cmd.get("chart_meta"),
                    timeframe=cmd.get("timeframe_str", "1D"),
                    limit=cmd.get("limit") or DEFAULT_CHART_LIMIT,
                    position=cmd.get("position"),
                    size=cmd.get("size"),
                    topbar_metrics=cmd.get("topbar_metrics"),
                    window_id=cmd.get("window_id"),
                )

                # Extract and execute as OPEN_WINDOW
                ds_symbol = display_cmd["symbol"]
                ds_tf = display_cmd["timeframe"]
                ds_win_id = display_cmd["window_id"]
                ds_sync_group = display_cmd.get("sync_group_id", "stocks")

                agent.open_window(
                    window_id=ds_win_id,
                    symbol=ds_symbol,
                    timeframe_unit=ds_tf.get("unit", "D"),
                    multiplier=ds_tf.get("multiplier", 1),
                    sync_group_id=ds_sync_group,
                    position=display_cmd.get("position"),
                    size=display_cmd.get("size"),
                )
                if ds_win_id in agent.layout_ledger and cmd.get("preset"):
                    agent.layout_ledger[ds_win_id]["preset"] = cmd.get("preset")

                # Chart-Definition im Ledger ablegen: sie ist die
                # Source of Truth fuer Symbolwechsel und fuer den Builder.
                if ds_win_id in agent.layout_ledger:
                    chart_info = display_cmd.get("chart") or {}
                    # Immer die AUFGELOESTE Panestruktur im Ledger ablegen:
                    # sie enthaelt die vom Server vergebenen Slot-Ids und die
                    # eingebauten Panes (Volumen), also genau das, was im
                    # Fenster steht. Overrides kommen aus der Anfrage.
                    inline = cmd.get("panes") or []
                    overrides_by_key = {}
                    for idx, entry in enumerate(inline):
                        entry = entry if isinstance(entry, dict) else {}
                        if entry.get("overrides"):
                            key = entry.get("pane_id") or f"#{idx}"
                            overrides_by_key[key] = entry["overrides"]
                    pane_specs = []
                    for idx, pane in enumerate(display_cmd.get("panes") or []):
                        key = pane.get("pane_id") or f"#{idx}"
                        pane_specs.append(
                            {
                                "pane_id": pane.get("pane_id"),
                                "pane_preset_id": pane.get("preset_id"),
                                "scale": pane.get("scale"),
                                "weight": pane.get("weight"),
                                "overrides": overrides_by_key.get(key, {}),
                            }
                        )
                    ledger = agent.layout_ledger[ds_win_id]
                    ledger["chart"] = {
                        "id": chart_info.get("id") or cmd.get("chart") or cmd.get("preset"),
                        "display_name": chart_info.get("display_name") or cmd.get("chart") or cmd.get("preset"),
                        "draft": bool(chart_info.get("draft")),
                        "panes": pane_specs,
                        "topbar_metrics": display_cmd.get("topbar_metrics") or [],
                        "x_axis_pane": display_cmd.get("x_axis_pane"),
                    }

                topbar = display_cmd.get("topbar")
                topbar_block = None
                if topbar:
                    topbar_block = {
                        "block_id": topbar.get("block_id", "info_block"),
                        "position": {"row": 0, "col": 0},
                        "content": topbar.get("content", ""),
                    }

                snap = {
                    "symbol": ds_symbol,
                    "timeframe": ds_tf,
                    "sync_group_id": ds_sync_group,
                    "bars": display_cmd["bars"],
                    "overlays": display_cmd.get("overlays", []),
                    "annotations": display_cmd.get("annotations", []),
                    # Y-Skalierung je Pane aus dem Preset (linear/log)
                    "pane_scales": display_cmd.get("pane_scales", {}),
                    # Pane-Ebene: Reihenfolge, Rollen, Titel, Gewichte
                    "panes": display_cmd.get("panes", []),
                    "chart": display_cmd.get("chart", {}),
                    "x_axis_pane": display_cmd.get("x_axis_pane"),
                    # Persist topbar blocks so they survive a viewer reconnect /
                    # layout.restore (the live push below is not persisted).
                    "topbar_blocks": [topbar_block] if topbar_block else [],
                }
                agent.send_snapshot(ds_win_id, snap)

                # Live push too, so the info row shows up immediately.
                if topbar_block:
                    from chart_viewer.models.envelope import make_envelope as mk_env, MessageKind as MK
                    tb_env = mk_env(
                        msg_type="topbar.set_block",
                        payload=topbar_block,
                        kind=MK.COMMAND,
                        window_id=ds_win_id,
                    )
                    server_transport.send_command(tb_env)

                result = {
                    "status": "ok",
                    "action": action,
                    "window_id": ds_win_id,
                    "bars": len(display_cmd["bars"]),
                    "overlays": len(display_cmd.get("overlays", [])),
                    # Warnkanal (Plan Phase 4): nie "success" fuer ein halbes Chart.
                    "warnings": display_cmd.get("warnings") or [],
                    "skipped": display_cmd.get("skipped") or [],
                }
            except Exception as de:
                logger.error(f"DISPLAY_STOCK failed: {de}")
                result = {"error": f"DISPLAY_STOCK failed: {str(de)}"}

    elif action in ("SCREENSHOT", "CAPTURE_SCREENSHOT"):
        if not server_transport._clients:
            result = {"error": "No Desktop Viewer clients connected"}
        else:
            try:
                import base64
                import os
                import time
                from chart_viewer.config import ViewerConfig
                server_config = ViewerConfig.from_env()

                is_hires = bool(
                    cmd.get("hires")
                    or cmd.get("resolution") in ("hires", "800x600")
                    or cmd.get("mode") == "hires"
                )
                default_w = server_config.screenshot_hires_width if is_hires else server_config.screenshot_width
                default_h = server_config.screenshot_hires_height if is_hires else server_config.screenshot_height

                timeout = float(cmd.get("timeout_sec", server_config.screenshot_timeout_sec))
                width = int(cmd.get("width", default_w))
                height = int(cmd.get("height", default_h))
                mode = cmd.get("mode", server_config.screenshot_mode)
                sharpen_amount = float(cmd.get("sharpen_amount", server_config.screenshot_sharpen_amount))
                out_dir = cmd.get("output_dir", server_config.screenshot_output_dir)

                os.makedirs(out_dir, exist_ok=True)

                snap_res = agent.request_screenshots(
                    window_id=win_id,
                    width=width,
                    height=height,
                    mode=mode,
                    timeout_s=timeout,
                    sharpen_amount=sharpen_amount,
                    hires=is_hires,
                )


                capture_id = snap_res.get("request_id", f"snap_{int(time.time())}")
                saved_files = []

                for s in snap_res.get("screenshots", []):
                    w_id = s.get("window_id", "win")
                    b64_data = s.get("image_base64", "")
                    if not b64_data:
                        continue

                    clean_win_id = str(w_id).replace(":", "_").replace("/", "_").replace("\\", "_")
                    filename = f"screenshot_{capture_id}_{clean_win_id}.png"
                    filepath = os.path.join(out_dir, filename)

                    img_bytes = base64.b64decode(b64_data)
                    with open(filepath, "wb") as f:
                        f.write(img_bytes)

                    saved_files.append({
                        "window_id": w_id,
                        "symbol": s.get("symbol", ""),
                        "filename": filename,
                        "filepath": filepath,
                        "width": s.get("width", width),
                        "height": s.get("height", height),
                        "bytes": len(img_bytes),
                    })

                result = {
                    "status": "ok",
                    "action": action,
                    "capture_id": capture_id,
                    "count": len(saved_files),
                    "output_dir": out_dir,
                    "files": saved_files,
                }
            except Exception as se:
                logger.error(f"Screenshot failed: {se}")
                result = {"error": f"Screenshot failed: {str(se)}"}

    # ── Window Setup Management ──────────────────────────
    elif action == "SAVE_SETUP":
        setup_name = cmd.get("setup_name")
        if not setup_name:
            result = {"error": "Missing 'setup_name' parameter"}
        else:
            try:
                save_result = agent.save_setup(setup_name)
                result = save_result
                # Optionale Slot-Bindungen direkt mitschreiben (Plan Phase 6):
                # windows=[{window_id|slot, chart_id, symbol, timeframe}]
                pins = cmd.get("windows")
                if isinstance(pins, list) and pins:
                    pinned = []
                    for pin in pins:
                        pin = pin if isinstance(pin, dict) else {}
                        try:
                            pinned.append(
                                agent.assign_setup_slot(
                                    setup_name,
                                    monitor_count=save_result.get("monitor_count"),
                                    window_id=pin.get("window_id"),
                                    slot=pin.get("slot"),
                                    chart_id=pin.get("chart_id"),
                                    symbol=pin.get("symbol"),
                                    timeframe=pin.get("timeframe"),
                                )
                            )
                        except Exception as pin_err:
                            pinned.append({"window_id": pin.get("window_id"), "error": str(pin_err)})
                    result = {**save_result, "pinned": pinned}
            except Exception as e:
                logger.error(f"SAVE_SETUP failed: {e}")
                result = {"error": str(e)}

    elif action == "LOAD_SETUP":
        setup_name = cmd.get("setup_name")
        if not setup_name:
            result = {"error": "Missing 'setup_name' parameter"}
        else:
            current_monitors = len(agent.screens) if agent.screens else 1

            try:
                load_result = agent.load_setup(setup_name, current_monitor_count=current_monitors)
                windows_to_open = load_result.get("windows", [])
                if not windows_to_open:
                    result = {"error": f"Setup '{setup_name}' contains no windows to load"}
                else:
                    close_existing = load_result.get("close_existing", True)

                    # Separate existing windows by type to prevent type confusion
                    existing_charts = [
                        wid for wid, win in agent.layout_ledger.items()
                        if "list_id" not in win and not wid.startswith("wl_")
                    ]
                    existing_watchlists = [
                        wid for wid, win in agent.layout_ledger.items()
                        if "list_id" in win or wid.startswith("wl_")
                    ]
                    all_existing_ids = set(agent.layout_ledger.keys())
                    keep_ids = set()
                    mapping = {}
                    # Setup v2: gemerkten Inhalt (Chart, Symbol, Timeframe)
                    # nach dem Oeffnen rendern. Der Selbst-POST laeuft in
                    # einem Thread, weil der HTTP-Server einreihig ist.
                    pending_renders = []

                    for w in windows_to_open:
                        w_type = w.get("window_type", "chart")
                        logical_id = w.get("window_id", "")
                        pos = w.get("position", {})
                        size = w.get("size", {})

                        if w_type == "chart":
                            if existing_charts:
                                reuse_id = existing_charts.pop(0)
                                mapping[logical_id] = reuse_id
                                keep_ids.add(reuse_id)
                            else:
                                import uuid
                                new_id = f"win_{uuid.uuid4().hex[:8]}"
                                mapping[logical_id] = new_id
                                keep_ids.add(new_id)

                            actual_id = mapping[logical_id]
                            agent.open_window(
                                window_id=actual_id,
                                symbol="CHART",
                                position=pos if pos else None,
                                size=size if size else None,
                            )
                            if actual_id in agent.layout_ledger:
                                agent.layout_ledger[actual_id]["color_flag"] = w.get("color_flag", 0)
                                if w.get("symbol") and (w.get("chart_id") or w.get("chart_panes")):
                                    render_cmd = {
                                        "action": "DISPLAY_STOCK",
                                        "window_id": actual_id,
                                        "symbol": w.get("symbol"),
                                        "timeframe_str": w.get("timeframe") or "1D",
                                        "position": pos,
                                        "size": size,
                                    }
                                    if w.get("chart_panes"):
                                        render_cmd["panes"] = w.get("chart_panes")
                                        render_cmd["chart_meta"] = {
                                            "id": w.get("chart_id"),
                                            "display_name": w.get("chart_name") or "Setup",
                                            "draft": not bool(w.get("chart_id")),
                                            "topbar_metrics": w.get("topbar_metrics") or [],
                                            "x_axis_pane": w.get("x_axis_pane"),
                                        }
                                    else:
                                        render_cmd["chart"] = w.get("chart_id")
                                        if w.get("topbar_metrics"):
                                            render_cmd["topbar_metrics"] = w.get("topbar_metrics")
                                    pending_renders.append(render_cmd)
                        else:
                            if existing_watchlists:
                                reuse_id = existing_watchlists.pop(0)
                                mapping[logical_id] = reuse_id
                                keep_ids.add(reuse_id)
                            else:
                                import uuid
                                new_id = f"wl_{uuid.uuid4().hex[:8]}"
                                mapping[logical_id] = new_id
                                keep_ids.add(new_id)

                            actual_id = mapping[logical_id]
                            agent.open_watchlist(
                                list_id=actual_id,
                                display_name=actual_id,
                                columns=[],
                                rows=[],
                                color_flag=w.get("color_flag", 0),
                                position=pos if pos else None,
                                size=size if size else None,
                            )

                    if close_existing:
                        for wid in list(all_existing_ids):
                            if wid not in keep_ids:
                                agent.layout_ledger.pop(wid, None)
                                from chart_viewer.models.envelope import make_envelope, MessageKind
                                close_env = make_envelope(
                                    msg_type="window.command",
                                    payload={"command": "close"},
                                    kind=MessageKind.COMMAND,
                                    window_id=wid,
                                )
                                server_transport.send_command(close_env)

                    agent.current_setup_name = setup_name

                    if pending_renders:
                        def _render_setup_windows(items):
                            import time as _time
                            import urllib.request as _url
                            _time.sleep(0.4)  # Antwort erst rausschicken
                            for item in items:
                                try:
                                    req = _url.Request(
                                        "http://127.0.0.1:8766/api/command",
                                        data=json.dumps(item).encode(),
                                        headers={"Content-Type": "application/json"},
                                    )
                                    _url.urlopen(req, timeout=120).read()
                                except Exception as exc:
                                    logger.warning(f"Setup-Render fehlgeschlagen: {exc}")

                        threading.Thread(
                            target=_render_setup_windows,
                            args=(pending_renders,),
                            name="cv-setup-render",
                            daemon=True,
                        ).start()

                    result = {
                        "status": "ok",
                        "action": "LOAD_SETUP",
                        "setup_name": setup_name,
                        "mapping": mapping,
                        "windows_opened": len(windows_to_open),
                        "windows_rendered": len(pending_renders),
                    }
            except Exception as e:
                logger.error(f"LOAD_SETUP failed: {e}")
                result = {"error": str(e)}

    elif action == "LIST_SETUPS":
        try:
            setups = agent.list_setups()
            result = {"status": "ok", "setups": setups, "count": len(setups)}
        except Exception as e:
            logger.error(f"LIST_SETUPS failed: {e}")
            result = {"error": str(e)}

    elif action == "DELETE_SETUP":
        setup_name = cmd.get("setup_name")
        if not setup_name:
            result = {"error": "Missing 'setup_name' parameter"}
        else:
            try:
                mc = cmd.get("monitor_count")
                del_result = agent.delete_setup(setup_name, monitor_count=mc)
                if agent.current_setup_name == setup_name:
                    agent.current_setup_name = None
                result = del_result
            except Exception as e:
                logger.error(f"DELETE_SETUP failed: {e}")
                result = {"error": str(e)}

    elif action == "RENAME_SETUP":
        setup_name = cmd.get("setup_name")
        new_setup_name = cmd.get("new_setup_name")
        if not setup_name or not new_setup_name:
            result = {"error": "Parameters 'setup_name' and 'new_setup_name' are required"}
        else:
            try:
                rename_result = agent.rename_setup(setup_name, new_setup_name)
                result = rename_result
            except Exception as e:
                logger.error(f"RENAME_SETUP failed: {e}")
                result = {"error": str(e)}

    elif action == "SETUP_ASSIGN":
        setup_name = cmd.get("setup_name")
        if not setup_name:
            result = {"error": "Missing 'setup_name' parameter"}
        else:
            try:
                result = agent.assign_setup_slot(
                    setup_name,
                    monitor_count=cmd.get("monitor_count"),
                    window_id=cmd.get("window_id"),
                    slot=cmd.get("slot"),
                    chart_id=cmd.get("chart_id"),
                    symbol=cmd.get("symbol"),
                    timeframe=cmd.get("timeframe"),
                )
            except Exception as e:
                logger.error(f"SETUP_ASSIGN failed: {e}")
                result = {"error": str(e)}

    elif action in _CONTROL_BRIDGE_ACTIONS:
        # Lifecycle-Ops: delegiert an den ControlService (Single Facade), damit
        # Control Panel und MCP dieselbe Implementierung benutzen.
        service = getattr(agent, "control_service", None)
        if service is None:
            result = {
                "ok": False,
                "error": {"code": "internal", "message": "Control-Service nicht verfuegbar"},
            }
        else:
            params = {k: v for k, v in cmd.items() if k != "action"}
            result = service.execute(action.lower(), params)

    else:
        result = {"error": f"Unknown action or missing window_id: {action}"}
    return result
