"""游标编解码单元测试：覆盖 ORM 原生类型导致 500 的回归。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import ValidationFailedError


def test_encode_cursor_accepts_uuid_and_datetime() -> None:
    now = datetime(2026, 11, 9, 12, 30, 45, 123456, tzinfo=UTC)
    user_id = uuid.uuid4()

    token = encode_cursor({"t": now, "id": user_id})
    decoded = decode_cursor(token)

    assert decoded == {"t": now.isoformat(), "id": str(user_id)}


def test_encode_cursor_accepts_isoformat_string_and_sequence() -> None:
    payload = {"t": "2026-11-09T12:30:45+00:00", "id": str(uuid.uuid4())}
    assert decode_cursor(encode_cursor(payload)) == payload
    assert decode_cursor(encode_cursor({"seq": 42})) == {"seq": 42}


def test_encode_cursor_rejects_unknown_types() -> None:
    with pytest.raises(TypeError):
        encode_cursor({"id": object()})


def test_decode_cursor_rejects_invalid_value() -> None:
    with pytest.raises(ValidationFailedError):
        decode_cursor("not-a-valid-cursor")


def test_decode_cursor_returns_empty_for_none() -> None:
    assert decode_cursor(None) == {}
    assert decode_cursor("") == {}
