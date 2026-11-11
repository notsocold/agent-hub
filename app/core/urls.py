"""离线 URL 校验工具。

目前用于 A2A ``authorization_url`` 白名单：远程 Agent 返回的授权链接属于不可信数据，
必须在落库与展示两处过滤，避免 ``javascript:``/``data:``、私网地址等出现在 API/SSE 中。

本模块刻意不依赖 ``app.core.ssrf``：SSRF 校验需要 DNS 解析（连接前确认目标地址），
而展示层的白名单必须纯离线、零网络，因此这里只用 ``urllib.parse`` + ``ipaddress``
做静态判断。规则：

- 仅允许 ``https``；``dev``/``test`` 额外允许指向 ``localhost``/``127.0.0.1`` 的 ``http``；
- 拒绝 URL 内嵌用户名/密码；
- 拒绝回环/私网/链路本地/保留/组播/未指定 IP 字面量（含 IPv4-mapped IPv6）；
- 拒绝 ``localhost``/``*.localhost``/``*.local``/``*.internal``（dev/test 例外见上）；
- 超长（>2000 字符）与畸形 URL 一律拒绝；
- ``allowed_hosts`` 非空时只放行精确匹配（大小写不敏感）的主机。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit, urlunsplit

# 与 ``auth_request.authorization_url`` 落库/展示的截断上限保持一致。
MAX_AUTHORIZATION_URL_LENGTH = 2000

# dev/test 下允许 http 的精确回环主机；其余私网/回环地址一律拒绝。
_DEV_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1"})

_DEV_ENVS = frozenset({"dev", "test"})

# 无法离线解析、但按惯例总是指向本机/内网的域名后缀。
_LOCAL_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".localhost", "localhost_host"),
    (".local", "local_suffix"),
    (".internal", "internal_suffix"),
)


def _normalize_host(hostname: str | None) -> str:
    """主机名归一化：小写并去掉 FQDN 尾点（``localhost.`` 不能绕过规则）。"""
    return (hostname or "").rstrip(".").lower()


def _invalid_host_chars(host: str) -> bool:
    """拒绝对浏览器/HTTP 客户端有歧义的空白与控制字符主机。"""
    return any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in host)


def _blocked_ip_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """返回 IP 字面量的拒绝原因；公网地址返回空串。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # 顺序有意义：Python 3.13 的 is_private 同时覆盖回环/链路本地，先给出更精确原因。
    if ip.is_unspecified:
        return "unspecified_ip"
    if ip.is_loopback:
        return "loopback_ip"
    if ip.is_link_local:
        return "link_local_ip"
    if ip.is_multicast:
        return "multicast_ip"
    if ip.is_reserved:
        return "reserved_ip"
    if ip.is_private:
        return "private_ip"
    return ""


def _normalize_allowed_hosts(allowed_hosts: list[str]) -> set[str]:
    return {
        item.strip().lower().rstrip(".")
        for item in allowed_hosts
        if isinstance(item, str) and item.strip()
    }


def evaluate_authorization_url(
    value: str | None, *, env: str, allowed_hosts: list[str]
) -> tuple[str | None, str]:
    """校验 ``authorization_url``，返回 ``(可用值或 None, 拒绝原因)``。

    合法时原因为空串；调用方可用原因写审计/日志。校验过程不联网。
    """
    if value is None:
        return None, "blank"
    if not isinstance(value, str):
        return None, "invalid_url"
    stripped = value.strip()
    if not stripped:
        return None, "blank"
    if len(stripped) > MAX_AUTHORIZATION_URL_LENGTH:
        return None, "too_long"
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return None, "invalid_url"

    scheme = parsed.scheme.lower()
    host = _normalize_host(parsed.hostname)
    dev_like = env in _DEV_ENVS
    dev_local_host = host in _DEV_HTTP_HOSTS
    if scheme == "https":
        pass
    elif scheme == "http" and dev_like and dev_local_host:
        pass
    elif scheme in {"http", "https"}:
        return None, "non_https"
    else:
        return None, "unsupported_scheme"

    if parsed.username is not None or parsed.password is not None:
        return None, "credentials_in_url"
    if not host:
        return None, "empty_host"
    if _invalid_host_chars(host):
        return None, "invalid_host"

    # dev/test 的本地回环例外只覆盖精确的 localhost / 127.0.0.1。
    if not (dev_like and dev_local_host):
        if host == "localhost":
            return None, "localhost_host"
        for suffix, reason in _LOCAL_SUFFIXES:
            if host.endswith(suffix):
                return None, reason
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            blocked = _blocked_ip_reason(ip)
            if blocked:
                return None, blocked

    allowed = _normalize_allowed_hosts(allowed_hosts)
    if allowed and host not in allowed:
        return None, "host_not_allowlisted"
    return stripped, ""


def validate_authorization_url(
    value: str | None, *, env: str, allowed_hosts: list[str]
) -> str | None:
    """返回可安全展示的 ``authorization_url``；违规返回 ``None``（不抛错）。

    调用方负责记录 warning/审计；展示层（历史脏数据）只过滤、不写审计。
    """
    return evaluate_authorization_url(value, env=env, allowed_hosts=allowed_hosts)[0]


def redact_url_for_audit(value: str, *, limit: int = 200) -> str:
    """生成审计用的 URL 前缀：截断到 ``limit``，并抹掉 userinfo 中的凭据。"""
    stripped = value.strip()
    try:
        parsed = urlsplit(stripped)
    except ValueError:
        return stripped[:limit]
    if parsed.username is None and parsed.password is None:
        return stripped[:limit]
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    clean = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    return clean[:limit]


__all__ = [
    "MAX_AUTHORIZATION_URL_LENGTH",
    "evaluate_authorization_url",
    "redact_url_for_audit",
    "validate_authorization_url",
]
