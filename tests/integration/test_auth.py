"""认证契约测试。"""

from __future__ import annotations

import httpx

from tests.conftest import auth_headers, register_user


async def test_register_login_me_and_duplicate(client: httpx.AsyncClient) -> None:
    payload = await register_user(client, email="alice@example.com")
    assert payload["user"]["email"] == "alice@example.com"
    assert payload["token_type"] == "bearer"
    assert payload["expires_in"] == 900

    duplicate = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "ALICE@example.com",
            "password": "password123",
            "display_name": "重复",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"

    me = await client.get("/api/v1/auth/me", headers=auth_headers(payload["access_token"]))
    assert me.status_code == 200
    assert me.json()["role"] == "user"

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "password123"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]

    wrong = await client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "wrong-password"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "unauthorized"


async def test_refresh_rotation_and_logout(client: httpx.AsyncClient) -> None:
    registered = await register_user(client)
    old_refresh = registered["refresh_token"]

    rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert rotated.status_code == 200
    new_refresh = rotated.json()["refresh_token"]
    assert new_refresh != old_refresh

    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401

    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": new_refresh},
        headers=auth_headers(registered["access_token"]),
    )
    assert logout.status_code == 204

    after_logout = await client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
    assert after_logout.status_code == 401


async def test_update_me_and_change_password(client: httpx.AsyncClient) -> None:
    registered = await register_user(client, password="password123")
    headers = auth_headers(registered["access_token"])

    updated = await client.patch("/api/v1/auth/me", json={"display_name": "老赵"}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "老赵"

    wrong = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong", "new_password": "new-password-1"},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "invalid_password"

    changed = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "password123", "new_password": "new-password-1"},
        headers=headers,
    )
    assert changed.status_code == 204

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": registered["user"]["email"], "password": "new-password-1"},
    )
    assert login.status_code == 200

    # 改密后旧的 refresh token 全部失效
    old_refresh = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": registered["refresh_token"]}
    )
    assert old_refresh.status_code == 401


async def test_protected_endpoint_requires_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["request_id"]
    assert response.headers["X-Request-ID"] == body["error"]["request_id"]
