"""Binary wire envelope according to Section 3.1."""

from __future__ import annotations
import time
import uuid
from enum import IntEnum
from typing import Any
import msgspec


class MessageKind(IntEnum):
    EVENT = 0
    COMMAND = 1
    ACK = 2
    ERROR = 3


class Envelope(msgspec.Struct, array_like=True):
    protocol_version: str      # "1.0"
    message_id: bytes          # 16 bytes
    sequence: int              # u64, per-connection monotonically increasing
    sent_at: int               # epoch ms
    window_id: str | None
    kind: int                  # 0=event, 1=command, 2=ack, 3=error
    type: str                  # e.g. "bar.append"
    payload: Any               # typed payload or dict/object


# Pre-configured msgspec encoder/decoder for maximum throughput
ENCODER = msgspec.msgpack.Encoder()
DECODER = msgspec.msgpack.Decoder(type=Envelope)


def create_message_id() -> bytes:
    """Generate a 16-byte random message ID."""
    return uuid.uuid4().bytes


def make_envelope(
    msg_type: str,
    payload: Any = None,
    kind: int = MessageKind.EVENT,
    window_id: str | None = None,
    sequence: int = 0,
    message_id: bytes | None = None,
    protocol_version: str = "1.0",
) -> Envelope:
    """Convenience helper to construct a valid Envelope."""
    return Envelope(
        protocol_version=protocol_version,
        message_id=message_id or create_message_id(),
        sequence=sequence,
        sent_at=int(time.time() * 1000),
        window_id=window_id,
        kind=kind,
        type=msg_type,
        payload=payload,
    )


def encode_envelope(envelope: Envelope) -> bytes:
    """Encode envelope to binary MessagePack bytes."""
    return ENCODER.encode(envelope)


def decode_envelope(data: bytes) -> Envelope:
    """Decode binary MessagePack bytes to Envelope."""
    return DECODER.decode(data)
