"""不依赖数据库的单元测试。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import UnauthorizedError, ValidationFailedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.modules.auth.schemas import RegisterRequest
from app.modules.chat.schemas import ChatStreamRequest


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("password123")
    assert hashed != "password123"
    assert verify_password("password123", hashed)
    assert not verify_password("wrong-password", hashed)


def test_jwt_type_is_enforced() -> None:
    user_id = uuid.uuid4()
    access = create_access_token(user_id)
    refresh = create_refresh_token(user_id)
    assert decode_token(access.token, "access").user_id == user_id
    assert decode_token(refresh.token, "refresh").user_id == user_id
    with pytest.raises(UnauthorizedError):
        decode_token(access.token, "refresh")


def test_cursor_roundtrip_and_invalid() -> None:
    cursor = encode_cursor({"t": "2026-01-01T00:00:00+00:00", "id": "abc"})
    assert decode_cursor(cursor)["id"] == "abc"
    with pytest.raises(ValidationFailedError):
        decode_cursor("not-a-cursor")


def test_register_schema_normalizes_email() -> None:
    payload = RegisterRequest(
        email="  Alice@Example.COM ", password="password123", display_name=" Alice "
    )
    assert payload.email == "alice@example.com"
    assert payload.display_name == "Alice"
    with pytest.raises(ValidationError):
        RegisterRequest(email="bad", password="password123", display_name="x")


def test_chat_schema_trims_message() -> None:
    request = ChatStreamRequest(message="  hi  ")
    assert request.message == "hi"
    with pytest.raises(ValidationError):
        ChatStreamRequest(message="   ")
