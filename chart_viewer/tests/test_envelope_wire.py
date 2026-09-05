from chart_viewer.config import ViewerConfig
"""Tests for Envelope and msgspec serialization (Section 3)."""

from chart_viewer.models.envelope import (
    Envelope,
    MessageKind,
    make_envelope,
    encode_envelope,
    decode_envelope,
    create_message_id,
)


def test_envelope_creation_and_binary_roundtrip():
    msg_id = create_message_id()
    assert len(msg_id) == 16

    payload = {"symbol": "BTCUSDT", "timeframe": "1D", "bars_count": 500}
    env = make_envelope(
        msg_type="snapshot.full",
        payload=payload,
        kind=MessageKind.EVENT,
        window_id="win-1",
        sequence=42,
        message_id=msg_id,
    )

    assert env.protocol_version == "1.0"
    assert env.sequence == 42
    assert env.window_id == "win-1"
    assert env.type == "snapshot.full"
    assert env.kind == MessageKind.EVENT

    # Encode to binary msgpack
    binary_data = encode_envelope(env)
    assert isinstance(binary_data, bytes)

    # Decode back
    decoded = decode_envelope(binary_data)
    assert decoded.protocol_version == "1.0"
    assert decoded.message_id == msg_id
    assert decoded.sequence == 42
    assert decoded.window_id == "win-1"
    assert decoded.kind == MessageKind.EVENT
    assert decoded.type == "snapshot.full"
    assert decoded.payload == payload
