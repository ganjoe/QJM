"""Agent WebSocket Server daemon running on the host machine (0.0.0.0:8765)."""

from __future__ import annotations
import sys
import logging
import threading

from chart_viewer.transport.websocket_server import WebSocketServerTransport
from chart_viewer.agent.agent_client import ChartAgent
from chart_viewer.agent.command_api import handle_command

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AgentServer")


def _src_version():
    """Cheap, stable content version of the client src tree (mtime+size based).

    Mirrors the file set served by /api/sync (excludes __pycache__ and .pyc),
    so the Windows client can skip the download when nothing changed.
    """
    import hashlib
    import os
    base_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.abspath(os.path.join(base_dir, ".."))
    entries = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rel = os.path.relpath(p, src_dir).replace(os.sep, "/")
            entries.append((rel, st.st_size, st.st_mtime_ns))
    entries.sort()
    h = hashlib.sha256()
    for rel, size, mtime in entries:
        h.update(f"{rel}\0{size}\0{mtime}\n".encode("utf-8"))
    return h.hexdigest()


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
                    # Timeout: ein haengender Render darf den Thread nicht festhalten
                    urllib.request.urlopen(req, timeout=60).read()
                except Exception as e:
                    logger.error(f"Failed to load default chart: {e}")
            threading.Thread(target=send_default, daemon=True).start()
            
    agent.on_viewer_ready_callback = on_ready

    # Start HTTP Control API on port 8766 for MCP tools
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

            elif self.path == "/api/sync_version":
                try:
                    ver = _src_version()
                    body = ver.encode("ascii")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as e:
                    logger.exception(f"Error serving sync version: {e}")
                    self.send_response(500)
                    self.end_headers()

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
                    result = handle_command(cmd, agent, server_transport)
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

    # ThreadingHTTPServer, nicht HTTPServer: die Bridge-Ops (SAVE_CHART/APPLY_CHART)
    # rendern intern ueber denselben Endpunkt zurueck - mit einem single-threaded
    # Server wuerde das deadlocken.
    httpd = ThreadingHTTPServer(("0.0.0.0", 8766), ControlHandler)
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
