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
# 恰好 2001 字符：拒绝边界。
_OVERLONG_2001_URL = "https://example.com/" + "a" * 1981
# 明显超长：拒绝。
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
        ("https://sub.example.com/authorize", "prod", [], "https://sub.example.com/authorize"),
        # 尾点 FQDN 仍视为合法公网主机。
        ("https://example.com./authorize", "prod", [], "https://example.com./authorize"),
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
        # CGNAT / 非全球可达地址（Python 3.13 的 is_private/is_reserved 不含 100.64/10）。
        ("https://100.64.0.1/authorize", "prod", []),
        ("https://100.127.255.255/authorize", "prod", []),
        ("https://[::ffff:100.64.0.1]/authorize", "prod", []),
        # localhost / .localhost / .local / .internal / .localdomain 域名（含尾点绕过尝试）。
        ("https://api.localhost/authorize", "prod", []),
        ("https://localhost./authorize", "prod", []),
        ("https://svc.local/authorize", "prod", []),
        ("https://svc.local./authorize", "prod", []),
        ("https://keycloak.internal/authorize", "prod", []),
        ("https://keycloak.internal./authorize", "prod", []),
        ("https://localhost.localdomain/authorize", "prod", []),
        ("https://svc.localdomain/authorize", "prod", []),
        # 携带用户名/密码的 URL。
        ("https://user:pass@idp.example.com/a", "prod", []),
        ("https://user@idp.example.com/a", "prod", []),
        # 超长：2001 与 3020 字符都拒绝（2000 收、2001 拒的精确边界）。
        (_OVERLONG_2001_URL, "prod", []),
        (_OVERLONG_URL, "prod", []),
        # 白名单非空时只放行精确命中（含 dev 回环也必须命中）。
        ("https://evil.example.com/a", "prod", ["idp.example.com"]),
        ("http://localhost/a", "dev", ["idp.example.com"]),
        # 浏览器等价 IP 写法：缩写/整数/十六进制/八进制/百分号编码。
        ("https://127.1/", "prod", []),
        ("https://2130706433/", "prod", []),
        ("https://0x7f000001/", "prod", []),
        ("https://0177.0.0.1/", "prod", []),
        ("https://0xa9fea9fe/", "prod", []),
        # 混合十六进制/十进制分段（WHATWG 浏览器按 IPv4 解析，如 127.0x0.0.1）。
        ("https://127.0x0.0.1/", "prod", []),
        ("https://127.0.0.0x1/", "prod", []),
        ("https://127.0x0.0x1/", "prod", []),
        ("https://10.0.0x0.1/", "prod", []),
        ("https://192.168.0x1.1/", "prod", []),
        ("https://169.254.0x0.1/", "prod", []),
        ("https://0x7f.0x0.0x0.0x1/", "prod", []),
        ("https://127.0.0.1%2e/", "prod", []),
        ("https://%31%32%37.0.0.1/", "prod", []),
        # 反斜杠会被部分浏览器等价为路径分隔符。
        ("https://127.0.0.1\\/@evil.com/", "prod", []),
        # 全角数字/表意句号等非 ASCII 主机（IDNA/全角绕过）。
        ("https://１２７。０。０。１/", "prod", []),
        # 无点单标签主机（内网短名/约定后缀）。
        ("https://internal/", "prod", []),
        ("https://local/", "prod", []),
        ("https://internal/", "dev", []),
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
        ("https://localhost.localdomain/", "prod", [], "localdomain_suffix"),
        ("https://svc.localdomain/", "prod", [], "localdomain_suffix"),
        ("https://user:pass@idp.example.com/a", "prod", [], "credentials_in_url"),
        (_OVERLONG_2001_URL, "prod", [], "too_long"),
        (_OVERLONG_URL, "prod", [], "too_long"),
        ("https://evil.example.com/a", "prod", ["idp.example.com"], "host_not_allowlisted"),
        ("https://[::1", "prod", [], "invalid_url"),
        ("https://exa mple.com/a", "prod", [], "invalid_host"),
        # 浏览器等价 IP / 单标签主机 / 反斜杠 / 非 ASCII 的拒绝原因。
        ("https://127.1/", "prod", [], "obfuscated_ip"),
        ("https://2130706433/", "prod", [], "obfuscated_ip"),
        ("https://0x7f000001/", "prod", [], "obfuscated_ip"),
        ("https://0177.0.0.1/", "prod", [], "obfuscated_ip"),
        ("https://%31%32%37.0.0.1/", "prod", [], "loopback_ip"),
        ("https://127.0.0.1%2e/", "prod", [], "loopback_ip"),
        ("https://internal/", "prod", [], "single_label_host"),
        ("https://local/", "prod", [], "single_label_host"),
        ("https://127.0.0.1\\/@evil.com/", "prod", [], "backslash"),
        ("https://１２７。０。０。１/", "prod", [], "non_ascii_host"),
        # 混合十六进制分段（浏览器等价 IPv4）。
        ("https://127.0x0.0.1/", "prod", [], "obfuscated_ip"),
        ("https://0x7f.0x0.0x0.0x1/", "prod", [], "obfuscated_ip"),
        # CGNAT 100.64.0.0/10：Python 3.13 相关标志均为 False，但非全球可达。
        ("https://100.64.0.1/a", "prod", [], "non_global_ip"),
        ("https://100.127.255.255/a", "prod", [], "non_global_ip"),
        ("https://[::ffff:100.64.0.1]/a", "prod", [], "non_global_ip"),
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


@pytest.mark.parametrize(
    "value",
    [
        {"authorization_url": "https://user:supersecret@169.254.169.254/"},
        123,
        ["https://example.com/authorize"],
        b"https://example.com/authorize",
    ],
)
def test_non_string_value_rejected_as_invalid_url(value: object) -> None:
    """远程 metadata 不可信：非字符串值不能 str() 后当成正常 URL。"""
    result, reason = evaluate_authorization_url(value, env="prod", allowed_hosts=[])
    assert result is None
    assert reason == "invalid_url"


def test_audit_prefix_redacts_credentials() -> None:
    redacted = redact_url_for_audit("https://user:supersecret@169.254.169.254/latest/meta-data")
    assert redacted == "https://169.254.169.254/latest/meta-data"
    assert "supersecret" not in redacted
    assert "@" not in redacted


@pytest.mark.parametrize(
    "value",
    [
        "https://user:supersecret@[::1",
        "https://user:supersecret@[::1]x/",
        "https://user:supersecret@[zz]/",
    ],
)
def test_audit_prefix_redacts_credentials_in_malformed_url(value: str) -> None:
    """urlsplit 失败时也必须先剥离 userinfo，审计/日志不能出现密码。"""
    redacted = redact_url_for_audit(value)
    assert "supersecret" not in redacted
    assert "user:" not in redacted
    assert "@" not in redacted


def test_audit_prefix_strips_control_chars() -> None:
    """日志注入防护：前缀中不能保留换行等控制字符。"""
    redacted = redact_url_for_audit("https://example.com/a\nb\rc\x00d\u2028e")
    assert "\n" not in redacted
    assert "\r" not in redacted
    assert "\x00" not in redacted
    assert "\u2028" not in redacted
    assert redacted == "https://example.com/abcde"


def test_audit_prefix_drops_query_and_fragment() -> None:
    """审计前缀不保留 query/fragment，避免 ?token= 等敏感参数落审计。"""
    redacted = redact_url_for_audit(
        "https://example.com/authorize?token=supersecret#access_token=secret"
    )
    assert redacted == "https://example.com/authorize"
    assert "token" not in redacted
    # 畸形 URL 同样要丢掉 query/fragment。
    malformed = redact_url_for_audit("https://user:supersecret@[zz]/?token=abc")
    assert malformed == "https://[zz]/"
    assert "token" not in malformed


def test_audit_prefix_truncates() -> None:
    redacted = redact_url_for_audit("https://example.com/" + "a" * 500)
    assert len(redacted) == 200
    assert redacted.startswith("https://example.com/")


def test_allowed_hosts_config_property() -> None:
    settings = Settings(a2a_auth_url_allowed_hosts=" IDP.Example.com, ,auth.other.com ,")
    assert settings.a2a_auth_url_allowed_host_list == ["idp.example.com", "auth.other.com"]
    assert Settings(a2a_auth_url_allowed_hosts="").a2a_auth_url_allowed_host_list == []
