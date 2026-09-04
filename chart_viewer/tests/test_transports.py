"""Tests for InProcess and WebSocket transports (Section 1, 3, 10)."""

import time
import threading
from chart_viewer.transport.in_process import create_in_process_pair
from chart_viewer.transport.websocket import WebSocketTransport
from chart_viewer.models.envelope import make_envelope, MessageKind, encode_envelope, decode_envelope
from chart_viewer.config import ViewerConfig
from websockets.sync.server import serve


def test_in_process_transport_communication():
    viewer_transport, agent_transport = create_in_process_pair()
    viewer_transport.connect()
    agent_transport.connect()

    received_by_agent = []
    received_by_viewer = []

    agent_transport.on_event(lambda env: received_by_agent.append(env))
    viewer_transport.on_event(lambda env: received_by_viewer.append(env))

    # Viewer sends command
    cmd = make_envelope("viewer.ready", payload={"screen": "1920x1080"}, kind=MessageKind.EVENT)
    viewer_transport.send_command(cmd)

    assert len(received_by_agent) == 1
    assert received_by_agent[0].type == "viewer.ready"
    assert received_by_agent[0].payload["screen"] == "1920x1080"

    # Agent responds with window.open
    open_win = make_envelope("window.open", payload={"window_id": "win-1", "symbol": "BTC"}, kind=MessageKind.COMMAND)
    agent_transport.send_command(open_win)

    assert len(received_by_viewer) == 1
    assert received_by_viewer[0].type == "window.open"
    assert received_by_viewer[0].payload["window_id"] == "win-1"


def test_websocket_binary_transport():
    """Test actual WebSocket server and WebSocketTransport with binary msgpack frames."""
    received_on_server = []

    def ws_handler(ws):
        for msg in ws:
            env = decode_envelope(msg)
            received_on_server.append(env)
            # Echo back with ack
            ack = make_envelope(
                msg_type="ack",
                payload={"status": "ok"},
                kind=MessageKind.ACK,
                message_id=env.message_id,
                sequence=1,
            )
            ws.send(encode_envelope(ack))

    # Start server on ephemeral port
    server = serve(ws_handler, "127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    port = server.socket.getsockname()[1]
    ws_url = f"ws://127.0.0.1:{port}"

    cfg = ViewerConfig(reconnect_backoff_initial_ms=50, reconnect_backoff_max_ms=100)
    client_transport = WebSocketTransport(url=ws_url, config=cfg)

    received_on_client = []
    client_transport.on_event(lambda env: received_on_client.append(env))

    client_transport.connect()

    # Wait for handshake & ack
    for _ in range(20):
        if len(received_on_server) >= 1 and len(received_on_client) >= 1:
            break
        time.sleep(0.05)

    client_transport.disconnect()
    server.shutdown()

    # Initial handshake sent viewer.ready
    assert len(received_on_server) >= 1
    assert received_on_server[0].type == "viewer.ready"

    # Client received ACK
    assert len(received_on_client) >= 1
    assert received_on_client[0].kind == MessageKind.ACK
