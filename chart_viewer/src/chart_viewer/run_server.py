"""Agent WebSocket Server daemon running on the host machine (0.0.0.0:8765)."""

from __future__ import annotations
import sys
import logging
import threading

from chart_viewer.transport.websocket_server import WebSocketServerTransport
from chart_viewer.agent.agent_client import ChartAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AgentServer")


def main():
    port = 8765
    server_transport = WebSocketServerTransport(host="0.0.0.0", port=port)
    agent = ChartAgent(transport=server_transport)

    # Start WebSocket server
    agent.start()
    logger.info(f"=== Chart Agent WebSocket Server active on ws://0.0.0.0:{port} ===")
    logger.info("Waiting for Desktop Viewer clients (e.g. from your Windows PC)...")

    import threading
    import urllib.request
    
    def on_ready():
        if not agent.layout_ledger:
            logger.info("No active windows on connect, triggering default chart from last state...")
            def send_default():
                try:
                    import os
                    payload = b'{"action":"DISPLAY_STOCK","symbol":"NVDA","preset":"qmaggi"}'
                    if os.path.exists("/tmp/last_chart_state.json"):
                        with open("/tmp/last_chart_state.json", "rb") as f:
                            payload = f.read()
                    
                    req = urllib.request.Request(
                        "http://127.0.0.1:8766/api/command",
                        data=payload,
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(req)
                except Exception as e:
                    logger.error(f"Failed to load default chart: {e}")
            threading.Thread(target=send_default, daemon=True).start()
            
    agent.on_viewer_ready_callback = on_ready

    # Start HTTP Control API on port 8766 for MCP tools
    from http.server import HTTPServer, BaseHTTPRequestHandler
    import json

    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Suppress default noisy request logging

        def do_GET(self):
            if self.path == "/api/status" or self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                res = {
                    "status": "healthy",
                    "clients_connected": len(server_transport._clients),
                    "open_windows": list(agent.layout_ledger.keys()),
                    "layout_ledger": agent.layout_ledger,
                }
                self.wfile.write(json.dumps(res).encode("utf-8"))

            elif self.path == "/api/sync" or self.path == "/api/sync_bundle":
                import io, tarfile, os
                try:
                    base_dir = os.path.dirname(os.path.abspath(__file__))
                    src_dir = os.path.abspath(os.path.join(base_dir, ".."))
                    buf = io.BytesIO()
                    with tarfile.open(fileobj=buf, mode="w") as tar:
                        tar.add(src_dir, arcname="src", filter=lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti)
                    data = buf.getvalue()

                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-tar")
                    self.send_header("Content-Disposition", 'attachment; filename="src_bundle.tar"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    logger.exception(f"Error serving sync bundle: {e}")
                    self.send_response(500)
                    self.end_headers()

            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/api/command":
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len)
                try:
                    cmd = json.loads(body.decode("utf-8"))
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

                    elif action == "DISPLAY_STOCK":
                        # Save the command state so it can be restored on connect/restart
                        try:
                            with open("/tmp/last_chart_state.json", "w") as f:
                                json.dump(cmd, f)
                        except Exception as e:
                            logger.warning(f"Failed to save last_chart_state: {e}")

                        # Full orchestration: fetch data from PCA-Service, build overlays, push to viewer
                        from chart_viewer.orchestrator import build_display_stock
                        symbol = cmd.get("symbol")
                        if not symbol:
                            result = {"error": "Missing 'symbol' parameter"}
                        else:
                            try:
                                display_cmd = build_display_stock(
                                    symbol=symbol,
                                    indicators=cmd.get("indicators"),
                                    preset=cmd.get("preset"),
                                    timeframe=cmd.get("timeframe_str", "1D"),
                                    limit=cmd.get("limit") or 1500,
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

                                snap = {
                                    "symbol": ds_symbol,
                                    "timeframe": ds_tf,
                                    "sync_group_id": ds_sync_group,
                                    "bars": display_cmd["bars"],
                                    "overlays": display_cmd.get("overlays", []),
                                    "annotations": display_cmd.get("annotations", []),
                                }
                                agent.send_snapshot(ds_win_id, snap)

                                # Set topbar if provided
                                topbar = display_cmd.get("topbar")
                                if topbar:
                                    from chart_viewer.models.envelope import make_envelope as mk_env, MessageKind as MK
                                    tb_env = mk_env(
                                        msg_type="topbar.set_block",
                                        payload={
                                            "block_id": topbar.get("block_id", "info_block"),
                                            "position": {"row": 0, "col": 0},
                                            "content": topbar.get("content", ""),
                                        },
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
                                from chart_viewer.config import GLOBAL_CONFIG

                                is_hires = bool(
                                    cmd.get("hires")
                                    or cmd.get("resolution") in ("hires", "800x600")
                                    or cmd.get("mode") == "hires"
                                )
                                default_w = GLOBAL_CONFIG.screenshot_hires_width if is_hires else GLOBAL_CONFIG.screenshot_width
                                default_h = GLOBAL_CONFIG.screenshot_hires_height if is_hires else GLOBAL_CONFIG.screenshot_height

                                timeout = float(cmd.get("timeout_sec", GLOBAL_CONFIG.screenshot_timeout_sec))
                                width = int(cmd.get("width", default_w))
                                height = int(cmd.get("height", default_h))
                                mode = cmd.get("mode", GLOBAL_CONFIG.screenshot_mode)
                                sharpen_amount = float(cmd.get("sharpen_amount", GLOBAL_CONFIG.screenshot_sharpen_amount))
                                out_dir = cmd.get("output_dir", GLOBAL_CONFIG.screenshot_output_dir)

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

                    else:
                        result = {"error": f"Unknown action or missing window_id: {action}"}


                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(result).encode("utf-8"))

                except Exception as e:
                    logger.error(f"Error handling HTTP command: {e}")
                    self.send_response(500)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

    httpd = HTTPServer(("0.0.0.0", 8766), ControlHandler)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    http_thread.start()
    logger.info("=== Chart Agent HTTP Control API active on http://0.0.0.0:8766 ===")


    stop_event = threading.Event()
    try:
        stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
        server_transport.disconnect()
        httpd.shutdown()


if __name__ == "__main__":
    main()
