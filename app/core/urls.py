"""离线 URL 校验工具。

目前用于 A2A ``authorization_url`` 白名单：远程 Agent 返回的授权链接属于不可信数据，
必须在落库与展示两处过滤，避免 ``javascript:``/``data:``、私网地址、浏览器等价 IP 写法等
出现在 API/SSE 中。

本模块刻意不依赖 ``app.core.ssrf``：SSRF 校验需要 DNS 解析（连接前确认目标地址），
而展示层的白名单必须纯离线、零网络，因此这里只用 ``urllib.parse`` + ``ipaddress``
做静态判断。规则：

- 仅允许 ``https``；``dev``/``test`` 额外允许指向字面量 ``localhost``/``127.0.0.1`` 的 ``http``；
- 拒绝 URL 内嵌用户名/密码；
- 拒绝回环/私网/链路本地/保留/组播/未指定 IP 字面量（含 IPv4-mapped IPv6）；
- 拒绝 ``localhost``/``*.localhost``/``*.local``/``*.internal`` 及其他单标签主机
  （dev/test 例外仅限字面量 ``localhost``/``127.0.0.1``）；
- 拒绝浏览器会等价解释为 IPv4 的数字写法（``127.1``、``2130706433``、``0x7f000001``、
  含八进制段等）；
- 主机先做百分号解码；URL 含反斜杠、主机含非 ASCII 或控制字符时一律拒绝；
- 超长（>2000 字符）与畸形 URL 一律拒绝；
- ``allowed_hosts`` 非空时只放行精确匹配（大小写不敏感）的主机。白名单只收窄、不放宽：
  即使命中白名单，私网/回环等主机仍会被拒绝。
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit, urlunsplit

# 与 ``auth_request.authorization_url`` 落库/展示的截断上限保持一致。
MAX_AUTHORIZATION_URL_LENGTH = 2000

# dev/test 下允许 http/https 的精确回环主机；其余私网/回环地址一律拒绝。
_DEV_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1"})

_DEV_ENVS = frozenset({"dev", "test"})

# 无法离线解析、但按惯例总是指向本机/内网的域名后缀。
_LOCAL_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".localhost", "localhost_host"),
    (".local", "local_suffix"),
    (".localdomain", "localdomain_suffix"),
    (".internal", "internal_suffix"),
)

# 审计前缀脱敏不能依赖 urlsplit（畸形 URL 会抛 ValueError），因此先用正则剥离 userinfo。
_URL_USERINFO_RE = re.compile(r"//[^/?#]*@")
_USERPASS_RE = re.compile(r"[^\s/?#:@]+:[^\s/?#@]*@")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_NUMERIC_HOST_RE = re.compile(r"^[0-9.]+$")
# WHATWG URL 解析允许 IPv4 段为十进制或 0x 十六进制，混用同样会被浏览器等价成 IP。
_IPV4_SEGMENT_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|[0-9]+)$")

# 解码后仍出现这些字符的主机名不可信（含双重编码残留的百分号）。
_INVALID_HOST_CHARS = frozenset("/\\%?#@")


def _normalize_host(hostname: str | None) -> str:
    """主机名归一化：百分号解码、小写、去掉 FQDN 尾点。

    ``%31%32%37.0.0.1`` / ``127.0.0.1%2e`` 归一化后都会还原为 IP 字面量，
    不能靠百分号编码绕过私网/回环判断。
    """
    return unquote(hostname or "").rstrip(".").lower()


def _invalid_host_chars(host: str) -> bool:
    """拒绝对浏览器/HTTP 客户端有歧义的字符主机。"""
    return any(
        ch in _INVALID_HOST_CHARS
        or ch.isspace()
        or ord(ch) < 0x20
        or ord(ch) == 0x7F
        for ch in host
    )


def _has_non_ascii(host: str) -> bool:
    """非 ASCII 主机直接拒绝，避免 IDNA/全角字符与规则判断不一致。"""
    return any(ord(ch) > 0x7F for ch in host)


def _looks_like_obfuscated_ip(host: str) -> bool:
    """``ipaddress`` 不认、但浏览器可能按 WHATWG 规则解析为 IPv4 的写法。

    覆盖：纯数字/缩写（``2130706433``/``127.1``）、``0x`` 前缀、以及 1–4 段
    十进制与十六进制混用（``127.0x0.0.1``、``10.0.0x0.1`` 等）。
    """
    if _NUMERIC_HOST_RE.fullmatch(host) or host.startswith("0x"):
        return True
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return False
    return all(_IPV4_SEGMENT_RE.fullmatch(part) for part in parts)


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
    # CGNAT 100.64.0.0/10 等地址在 Python 3.13 的各标志均为 False，但并非全球可达。
    if not ip.is_global:
        return "non_global_ip"
    return ""


def _normalize_allowed_hosts(allowed_hosts: list[str]) -> set[str]:
    return {
        unquote(item.strip()).lower().rstrip(".")
        for item in allowed_hosts
        if isinstance(item, str) and item.strip()
    }


def _host_rejection_reason(host: str, *, dev_like: bool) -> str:
    """主机白名单校验：返回拒绝原因；通过返回空串。"""
    if not host:
        return "empty_host"
    if _invalid_host_chars(host):
        return "invalid_host"
    if _has_non_ascii(host):
        return "non_ascii_host"
    # dev/test 的本地回环例外只覆盖精确的 localhost / 127.0.0.1。
    if dev_like and host in _DEV_HTTP_HOSTS:
        return ""
    if host == "localhost":
        return "localhost_host"
    for suffix, reason in _LOCAL_SUFFIXES:
        if host.endswith(suffix):
            return reason

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return _blocked_ip_reason(ip)
    if _looks_like_obfuscated_ip(host):
        return "obfuscated_ip"
    if "." not in host:
        return "single_label_host"
    return ""


def evaluate_authorization_url(
    value: object, *, env: str, allowed_hosts: list[str]
) -> tuple[str | None, str]:
    """校验 ``authorization_url``，返回 ``(可用值或 None, 拒绝原因)``。

    非字符串值（远程 metadata 不可信）按 ``invalid_url`` 拒绝，绝不做 ``str()`` 后
    当成正常 URL 使用。合法时原因为空串；调用方可用原因写审计/日志。校验过程不联网。
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
    # 反斜杠会被部分浏览器等价为路径分隔符（如 https://host\/@evil.com），直接拒绝。
    if "\\" in stripped:
        return None, "backslash"
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
    host_reason = _host_rejection_reason(host, dev_like=dev_like)
    if host_reason:
        return None, host_reason

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


def _strip_query_fragment(text: str) -> str:
    """字符串层面去掉 query/fragment（畸形 URL 无法交给 urlsplit 时使用）。"""
    for separator in ("?", "#"):
        index = text.find(separator)
        if index != -1:
            text = text[:index]
    return text


def redact_url_for_audit(value: str, *, limit: int = 200) -> str:
    """生成审计用的 URL 前缀：去控制字符、截断到 ``limit``，抹掉 userinfo 与 query/fragment。

    不依赖 ``urlsplit`` 成功：畸形 URL（如 ``https://user:pass@[::1``）也会先剥离
    userinfo，保证 warning 日志与审计 detail 绝不出现 ``user:pass@``；query/fragment
    可能携带 ``?token=`` 等敏感参数，同样不进入审计前缀。
    """
    cleaned = _CONTROL_CHARS_RE.sub("", value.strip())
    cleaned = _URL_USERINFO_RE.sub("//", cleaned)
    cleaned = _USERPASS_RE.sub("", cleaned)
    try:
        parsed = urlsplit(cleaned)
    except ValueError:
        return _strip_query_fragment(cleaned)[:limit]
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        netloc = netloc.rsplit("@", 1)[-1]
    clean = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    return clean[:limit]


__all__ = [
    "MAX_AUTHORIZATION_URL_LENGTH",
    "evaluate_authorization_url",
    "redact_url_for_audit",
    "validate_authorization_url",
]
