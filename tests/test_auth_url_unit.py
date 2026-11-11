"""A2A ``authorization_url`` 白名单单元测试（纯离线，不连数据库、不发网络请求）。

覆盖规则：协议白名单、dev/test 本地回环例外、URL 凭据、私网/回环/链路本地 IP、
``.local``/``.internal`` 后缀、超长、大小写与首尾空白、``allowed_hosts``
为空/命中/未命中，以及审计前缀的截断与凭据脱敏。
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.urls import (
    evaluate_authorization_url,
    redact_url_for_audit,
    validate_authorization_url,
)

_PUBLIC_HTTPS = "https://idp.example.com/authorize?client_id=agent-hub&state=abc"
# 恰好 2000 字符：合法边界。
_MAX_LEN_URL = "https://example.com/" + "a" * 1980
# 超过 2000 字符：拒绝。
_OVERLONG_URL = "https://example.com/" + "a" * 3000


def _validate(
    value: str | None, *, env: str = "prod", hosts: list[str] | None = None
) -> str | None:
    return validate_authorization_url(value, env=env, allowed_hosts=hosts or [])


@pytest.mark.parametrize(
    ("value", "env", "hosts", "expected"),
    [
        # 公网 https 在各环境都合法。
        (_PUBLIC_HTTPS, "prod", [], _PUBLIC_HTTPS),
        ("https://example.com", "dev", [], "https://example.com"),
        # 公网 IP 字面量（非私网段）允许。
        ("https://93.184.216.34/authorize", "prod", [], "https://93.184.216.34/authorize"),
        # dev/test 允许 http 回环（精确主机 localhost / 127.0.0.1）。
        ("http://localhost:8000/authorize", "dev", [], "http://localhost:8000/authorize"),
        ("http://127.0.0.1:8080/authorize", "test", [], "http://127.0.0.1:8080/authorize"),
        # dev/test 的 https 回环同样放行。
        ("https://localhost:8443/authorize", "test", [], "https://localhost:8443/authorize"),
        ("https://127.0.0.1/authorize", "dev", [], "https://127.0.0.1/authorize"),
        # 仅去首尾空白，保留原始大小写与内容。
        (
            "  https://Example.COM/Authorize?state=Abc  ",
            "prod",
            [],
            "https://Example.COM/Authorize?state=Abc",
        ),
        (
            "HTTPS://Example.COM/Authorize?state=Abc",
            "prod",
            [],
            "HTTPS://Example.COM/Authorize?state=Abc",
        ),
        # scheme/主机大小写不影响 dev 回环例外判断。
        ("HTTP://LOCALHOST:8000/authorize", "dev", [], "HTTP://LOCALHOST:8000/authorize"),
        # 白名单精确匹配、大小写与条目空白不敏感。
        ("https://idp.example.com/a", "prod", ["IDP.Example.com"], "https://idp.example.com/a"),
        (
            "https://idp.example.com/a",
            "prod",
            ["  idp.example.com  ", "auth.other.com"],
            "https://idp.example.com/a",
        ),
        # 尾点 FQDN 归一化后仍命中白名单。
        ("https://idp.example.com./a", "prod", ["idp.example.com"], "https://idp.example.com./a"),
        # 长度边界：恰好 2000 字符。
        (_MAX_LEN_URL, "prod", [], _MAX_LEN_URL),
    ],
)
def test_validate_authorization_url_accepts_valid(
    value: str, env: str, hosts: list[str], expected: str
) -> None:
    assert _validate(value, env=env, hosts=hosts) == expected


@pytest.mark.parametrize(
    ("value", "env", "hosts"),
    [
        # None / 空白不算合法 URL，直接收敛为 None。
        (None, "prod", []),
        ("", "prod", []),
        ("   ", "prod", []),
        # 非 https：公共主机在 prod/dev 都不行，只有 dev/test 的回环 http 例外。
        ("http://idp.example.com/authorize", "prod", []),
        ("http://idp.example.com/authorize", "dev", []),
        ("http://localhost/authorize", "prod", []),
        ("http://[::1]/authorize", "test", []),
        ("https://localhost/authorize", "prod", []),
        ("https://127.0.0.1/authorize", "prod", []),
        # 危险协议 / 无 scheme / 协议相对 URL（scheme 大小写不敏感）。
        ("javascript:alert(1)", "prod", []),
        ("JAVASCRIPT:alert(1)", "prod", []),
        ("data:text/html;base64,PHNjcmlwdD4=", "prod", []),
        ("file:///etc/passwd", "prod", []),
        ("ftp://example.com/authorize", "prod", []),
        ("example.com/authorize", "prod", []),
        ("//example.com/authorize", "prod", []),
        ("https:/example.com/authorize", "prod", []),
        # 私网 / 回环 / 链路本地 / 保留 / 组播 / 未指定 IP 字面量。
        ("https://10.0.0.5/authorize", "prod", []),
        ("https://172.16.0.1/authorize", "prod", []),
        ("https://192.168.1.10/authorize", "prod", []),
        ("https://169.254.169.254/latest/meta-data", "prod", []),
        ("https://169.254.169.254/latest/meta-data", "dev", []),
        ("https://[::1]/authorize", "prod", []),
        ("https://[::1]/authorize", "test", []),
        ("https://[fe80::1]/authorize", "prod", []),
        ("https://[::ffff:127.0.0.1]/authorize", "prod", []),
        ("https://0.0.0.0/authorize", "prod", []),
        ("https://224.0.0.1/authorize", "prod", []),
        ("https://240.0.0.1/authorize", "prod", []),
        # localhost / .localhost / .local / .internal 域名（含尾点绕过尝试）。
        ("https://api.localhost/authorize", "prod", []),
        ("https://localhost./authorize", "prod", []),
        ("https://svc.local/authorize", "prod", []),
        ("https://svc.local./authorize", "prod", []),
        ("https://keycloak.internal/authorize", "prod", []),
        ("https://keycloak.internal./authorize", "prod", []),
        # 携带用户名/密码的 URL。
        ("https://user:pass@idp.example.com/a", "prod", []),
        ("https://user@idp.example.com/a", "prod", []),
        # 超长。
        (_OVERLONG_URL, "prod", []),
        # 白名单非空时只放行精确命中（含 dev 回环也必须命中）。
        ("https://evil.example.com/a", "prod", ["idp.example.com"]),
        ("http://localhost/a", "dev", ["idp.example.com"]),
        # 畸形 URL / 主机。
        ("https://[::1", "prod", []),
        ("https://exa mple.com/a", "prod", []),
    ],
)
def test_validate_authorization_url_rejects_invalid(
    value: str | None, env: str, hosts: list[str]
) -> None:
    assert _validate(value, env=env, hosts=hosts) is None


@pytest.mark.parametrize(
    ("value", "env", "hosts", "reason"),
    [
        # 缺失/空白不是违规（调用方不应写审计）。
        (None, "prod", [], "blank"),
        ("   ", "prod", [], "blank"),
        ("javascript:alert(1)", "prod", [], "unsupported_scheme"),
        ("http://idp.example.com/a", "prod", [], "non_https"),
        ("https://10.0.0.5/a", "prod", [], "private_ip"),
        ("https://127.0.0.1/a", "prod", [], "loopback_ip"),
        ("https://169.254.169.254/a", "prod", [], "link_local_ip"),
        ("https://0.0.0.0/a", "prod", [], "unspecified_ip"),
        ("https://224.0.0.1/a", "prod", [], "multicast_ip"),
        ("https://240.0.0.1/a", "prod", [], "reserved_ip"),
        ("https://localhost/a", "prod", [], "localhost_host"),
        ("https://svc.local/a", "prod", [], "local_suffix"),
        ("https://svc.internal/a", "prod", [], "internal_suffix"),
        ("https://user:pass@idp.example.com/a", "prod", [], "credentials_in_url"),
        (_OVERLONG_URL, "prod", [], "too_long"),
        ("https://evil.example.com/a", "prod", ["idp.example.com"], "host_not_allowlisted"),
        ("https://[::1", "prod", [], "invalid_url"),
        ("https://exa mple.com/a", "prod", [], "invalid_host"),
    ],
)
def test_rejection_reason(
    value: str | None, env: str, hosts: list[str], reason: str
) -> None:
    result, actual_reason = evaluate_authorization_url(value, env=env, allowed_hosts=hosts)
    assert result is None
    assert actual_reason == reason


def test_evaluate_returns_empty_reason_on_success() -> None:
    value, reason = evaluate_authorization_url(_PUBLIC_HTTPS, env="prod", allowed_hosts=[])
    assert value == _PUBLIC_HTTPS
    assert reason == ""


def test_audit_prefix_redacts_credentials() -> None:
    redacted = redact_url_for_audit("https://user:supersecret@169.254.169.254/latest/meta-data")
    assert redacted == "https://169.254.169.254/latest/meta-data"
    assert "supersecret" not in redacted
    assert "@" not in redacted


def test_audit_prefix_truncates() -> None:
    redacted = redact_url_for_audit("https://example.com/" + "a" * 500)
    assert len(redacted) == 200
    assert redacted.startswith("https://example.com/")


def test_allowed_hosts_config_property() -> None:
    settings = Settings(a2a_auth_url_allowed_hosts=" IDP.Example.com, ,auth.other.com ,")
    assert settings.a2a_auth_url_allowed_host_list == ["idp.example.com", "auth.other.com"]
    assert Settings(a2a_auth_url_allowed_hosts="").a2a_auth_url_allowed_host_list == []
