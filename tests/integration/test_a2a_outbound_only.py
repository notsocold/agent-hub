"""A2A 出站专用契约（2026-11-10 需求变更）。

本服务只做 A2A Client：不发布 Agent Card、不暴露 JSON-RPC 入站端点。
本文件固定这条边界，防止后续误把入站路由加回来。
"""

from __future__ import annotations

import httpx

from tests.conftest import auth_headers, register_user

_EXPECTED_A2A_INFO = {
    "enabled": False,
    "card_url": "",
    "jsonrpc_url": "",
    "auth_type": "none",
    "card_security": "none",
}


async def test_system_info_reports_no_a2a_inbound(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    response = await client.get(
        "/api/v1/system/info", headers=auth_headers(registered["access_token"])
    )
    assert response.status_code == 200
    assert response.json()["a2a"] == _EXPECTED_A2A_INFO


async def test_a2a_inbound_routes_not_registered(client: httpx.AsyncClient) -> None:
    """well-known Agent Card 与 JSON-RPC 端点都不应存在。"""
    for path in ("/a2a/.well-known/agent-card.json", "/.well-known/agent-card.json"):
        response = await client.get(path)
        assert response.status_code == 404, f"{path} 不应注册入站路由"

    response = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "req-1", "method": "message/send", "params": {}},
    )
    assert response.status_code == 404
