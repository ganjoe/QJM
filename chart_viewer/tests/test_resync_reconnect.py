"""Tests for reconnect, sequence gap detection, and full state reset (Section 10 & Criterion 5)."""

import time
from chart_viewer.transport.websocket import WebSocketTransport
from chart_viewer.models.envelope import make_envelope, MessageKind, encode_envelope
from chart_viewer.config import ViewerConfig


def test_sequence_gap_triggers_resync_request():
    """Verify that receiving diverging sequence numbers triggers resync.request."""
    cfg = ViewerConfig(resync_rate_limit_sec=5.0)
    ws_transport = WebSocketTransport(config=cfg)

    sent_commands = []
    # Mock send_command to record outbound messages
    ws_transport.send_command = lambda env: sent_commands.append(env)

    # First normal frame: sequence 1
    env1 = make_envelope("bar.append", payload={"bar": {}}, sequence=1, window_id="win-1")
    ws_transport._handle_binary_frame(encode_envelope(env1))
    assert ws_transport._last_received_sequence == 1
    assert len(sent_commands) == 0

    # Normal frame: sequence 2
    env2 = make_envelope("bar.append", payload={"bar": {}}, sequence=2, window_id="win-1")
    ws_transport._handle_binary_frame(encode_envelope(env2))
    assert ws_transport._last_received_sequence == 2
    assert len(sent_commands) == 0

    # Gap! Receive sequence 5 instead of 3
    env_gap = make_envelope("bar.append", payload={"bar": {}}, sequence=5, window_id="win-1")
    ws_transport._handle_binary_frame(encode_envelope(env_gap))

    # Resync request must have been triggered!
    assert len(sent_commands) == 1
    resync_cmd = sent_commands[0]
    assert resync_cmd.type == "resync.request"
    assert resync_cmd.payload["reason"] == "sequence_gap"
    assert resync_cmd.window_id == "win-1"


def test_resync_rate_limiting():
    """Verify that multiple sequence gaps within 5 seconds trigger at most 1 resync request."""
    cfg = ViewerConfig(resync_rate_limit_sec=5.0)
    ws_transport = WebSocketTransport(config=cfg)

    sent_commands = []
    ws_transport.send_command = lambda env: sent_commands.append(env)

    # Initial sequence 1
    env_init = make_envelope("bar.append", payload={}, sequence=1, window_id="win-1")
    ws_transport._handle_binary_frame(encode_envelope(env_init))
    assert len(sent_commands) == 0

    # First gap: sequence 10 instead of 2
    env_gap1 = make_envelope("bar.append", payload={}, sequence=10, window_id="win-1")
    ws_transport._handle_binary_frame(encode_envelope(env_gap1))
    assert len(sent_commands) == 1

    # Second gap immediately after (within 5 seconds): sequence 20 instead of 11
    env_gap2 = make_envelope("bar.append", payload={}, sequence=20, window_id="win-1")
    ws_transport._handle_binary_frame(encode_envelope(env_gap2))

    # Must NOT trigger another resync.request due to rate limiting
    assert len(sent_commands) == 1
