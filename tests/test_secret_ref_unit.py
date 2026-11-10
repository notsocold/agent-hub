"""secret: 引用格式与 headers 解析的单元测试。"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationFailedError
from app.core.ssrf import normalize_headers, resolve_headers


def test_user_cannot_use_platform_secret_ref() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        normalize_headers({"Authorization": "secret:MY_KEY"})
    assert excinfo.value.code == "secret_ref_not_allowed"


def test_admin_can_use_platform_secret_ref() -> None:
    stored, hint = normalize_headers(
        {"Authorization": "secret:MY_KEY"}, allow_platform_secret=True
    )
    assert stored["Authorization"] == {"secret_ref": "secret:MY_KEY"}
    assert "secret:MY_KEY" in hint


def test_env_ref_still_supported() -> None:
    stored, hint = normalize_headers({"Authorization": "env:MY_KEY"})
    assert stored["Authorization"] == {"secret_ref": "env:MY_KEY"}
    assert "env:MY_KEY" in hint


def test_sync_resolve_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_KEY", "value-from-env")
    resolved = resolve_headers({"Authorization": {"secret_ref": "env:MY_KEY"}})
    assert resolved == {"Authorization": "value-from-env"}


async def test_async_resolve_reads_platform_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.ssrf import resolve_headers_async

    async def fake_resolve(db, name: str) -> str | None:
        return "plain-secret" if name == "MY_KEY" else None

    monkeypatch.setattr(
        "app.modules.secrets.service.resolve_platform_secret", fake_resolve
    )
    resolved = await resolve_headers_async(
        None, {"X-Api-Key": {"secret_ref": "secret:MY_KEY"}}
    )
    assert resolved == {"X-Api-Key": "plain-secret"}

    missing = await resolve_headers_async(
        None, {"X-Api-Key": {"secret_ref": "secret:OTHER"}}
    )
    assert missing == {}
