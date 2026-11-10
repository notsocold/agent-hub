"""聊天限流：按用户滑动窗口、用户隔离、开关关闭。"""

from __future__ import annotations

import httpx
import pytest

from app.core.config import reset_settings_cache
from app.core.rate_limit import limiter
from tests.conftest import auth_headers, register_user


@pytest.fixture
def chat_limit(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """打开限流并把聊天窗口收紧到 2 次/60 秒；用例结束恢复。"""
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("RATE_LIMIT_CHAT_MAX", "2")
    monkeypatch.setenv("RATE_LIMIT_CHAT_WINDOW_SECONDS", "60")
    reset_settings_cache()
    limiter.reset()
    yield
    reset_settings_cache()
    limiter.reset()


async def _send(client: httpx.AsyncClient, headers: dict[str, str]) -> httpx.Response:
    return await client.post("/api/v1/chat/stream", json={"message": "你好"}, headers=headers)


async def test_chat_rate_limit_blocks_third_message(
    client: httpx.AsyncClient, chat_limit: None
) -> None:
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    assert (await _send(client, headers)).status_code == 200
    assert (await _send(client, headers)).status_code == 200
    third = await _send(client, headers)
    assert third.status_code == 429
    error = third.json()["error"]
    assert error["code"] == "rate_limited"
    assert error["details"]["retry_after_seconds"] >= 1


async def test_chat_rate_limit_isolated_per_user(
    client: httpx.AsyncClient, chat_limit: None
) -> None:
    first = auth_headers((await register_user(client))["access_token"])
    second = auth_headers((await register_user(client))["access_token"])
    assert (await _send(client, first)).status_code == 200
    assert (await _send(client, first)).status_code == 200
    assert (await _send(client, first)).status_code == 429
    # 另一个用户不受影响
    assert (await _send(client, second)).status_code == 200


async def test_chat_rate_limit_disabled(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_CHAT_MAX", "2")
    reset_settings_cache()
    limiter.reset()
    try:
        headers = auth_headers((await register_user(client))["access_token"])
        for _ in range(5):
            assert (await _send(client, headers)).status_code == 200
    finally:
        reset_settings_cache()
        limiter.reset()
