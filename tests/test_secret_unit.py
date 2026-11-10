"""平台密钥的 name 校验、长度限制与加解密往返。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.ssrf import decrypt_secret, encrypt_secret
from app.modules.secrets.schemas import SecretCreateRequest, SecretUpdateRequest


def test_secret_name_validation() -> None:
    with pytest.raises(ValidationError):
        SecretCreateRequest(name="bad name", value="x")
    with pytest.raises(ValidationError):
        SecretCreateRequest(name="1abc", value="x")
    ok = SecretCreateRequest(name="DATA_AGENT_KEY", value="secret-value")
    assert ok.name == "DATA_AGENT_KEY"


def test_secret_encrypt_roundtrip() -> None:
    token = encrypt_secret("hello-secret")
    assert token is not None and token.startswith("v1:")
    assert decrypt_secret(token) == "hello-secret"


def test_secret_value_length_limits() -> None:
    with pytest.raises(ValidationError):
        SecretCreateRequest(name="KEY", value="")
    with pytest.raises(ValidationError):
        SecretCreateRequest(name="KEY", value="x" * 4097)


def test_update_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        SecretUpdateRequest()
    assert SecretUpdateRequest(enabled=False).enabled is False
