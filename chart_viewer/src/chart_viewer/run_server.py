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
