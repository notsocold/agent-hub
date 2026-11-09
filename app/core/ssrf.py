"""SSRF 主机校验与 MCP 凭据加密。

- SSRF：只允许 http(s)；连接前解析 IP，拒绝回环/私网/链路本地/组播/云元数据地址，
  允许通过白名单放行内网主机；TLS 校验开启；重定向由调用方统一禁止。
- 凭据：敏感请求头用 AES-GCM 加密存储（CAPABILITY_SECRET_KEY），密钥缺失时只允许
  ``env:`` 形式的 secret_ref；数据库中绝不落明文。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import re
import socket
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError, ValidationFailedError

logger = logging.getLogger(__name__)

# 凭据相关的请求头名（大小写不敏感）；普通头允许明文存储。
_SECRET_HEADER_NAMES = {
    "authorization",
    "x-api-key",
    "x-auth-token",
    "cookie",
    "token",
    "apikey",
    "private-token",
}

_SECRET_REF_PATTERN = re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$")

_METADATA_IPS = {"169.254.169.254", "fd00:ec2::254"}


def is_private_host(hostname: str) -> tuple[bool, str]:
    """判断主机是否属于内网/回环/链路本地等禁访地址。

    返回 ``(is_blocked, reason)``；解析失败视为禁访（防 DNS 重绑定绕过）。
    """
    hostname = hostname.strip().lower()
    if not hostname or hostname == "localhost":
        return True, "localhost 禁止访问"
    try:
        info = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return True, f"主机名无法解析：{exc}"
    if not info:
        return True, "主机名无法解析"
    for item in info:
        raw_ip = item[4][0]
        ip = ipaddress.ip_address(raw_ip)
        if ip.is_unspecified or ip.is_loopback or ip.is_link_local:
            return True, f"禁止访问回环/链路本地地址：{raw_ip}"
        if ip.is_private:
            return True, f"禁止访问内网地址：{raw_ip}"
        if ip.is_multicast or ip.is_reserved:
            return True, f"禁止访问保留地址：{raw_ip}"
        if str(ip) in _METADATA_IPS or str(ip).startswith("fd00:ec2::"):
            return True, f"禁止访问云元数据地址：{raw_ip}"
    return False, ""


def validate_mcp_url(url: str) -> str:
    """校验 MCP 服务器 URL：仅 http(s)、无凭据、主机非内网（白名单除外）。"""
    settings = get_settings()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationFailedError("仅支持 http/https 协议", code="mcp_ssrf_blocked")
    if parsed.username or parsed.password:
        raise ValidationFailedError("URL 中不允许携带用户名/密码", code="mcp_ssrf_blocked")
    hostname = parsed.hostname or ""
    allowlist = settings.mcp_host_allowlist
    if hostname.lower() in allowlist:
        return url
    blocked, reason = is_private_host(hostname)
    if blocked:
        raise ValidationFailedError(f"URL 主机校验失败：{reason}", code="mcp_ssrf_blocked")
    return url


def _host_in_allowlist(hostname: str, allowlist: list[str]) -> bool:
    """主机名精确匹配，或其解析 IP 命中白名单条目（IP 精确或 CIDR）。"""
    hostname = hostname.lower()
    if hostname in allowlist:
        return True
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    ips = {ipaddress.ip_address(item[4][0]) for item in infos}
    for entry in allowlist:
        if "/" in entry:
            try:
                network = ipaddress.ip_network(entry, strict=False)
            except ValueError:
                continue
            if any(ip in network for ip in ips):
                return True
        else:
            try:
                candidate = ipaddress.ip_address(entry)
            except ValueError:
                continue
            if candidate in ips:
                return True
    return False


def _is_loopback_host(hostname: str) -> bool:
    """localhost 或解析后全部地址均为回环。"""
    hostname = hostname.lower()
    if hostname == "localhost":
        return True
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    return all(ipaddress.ip_address(item[4][0]).is_loopback for item in infos)


def validate_a2a_url(url: str) -> str:
    """A2A 出站 URL 校验：协议、无凭据、SSRF 主机检查。

    - 仅 http(s)；prod 环境只允许 https；
    - 主机命中 ``A2A_ALLOWED_HOSTS`` 白名单（主机名/IP/CIDR）放行；
    - ``A2A_ALLOW_LOOPBACK``（dev/test 默认允许）放行回环地址，方便本地假 Agent；
    - 其余按内网/链路本地/云元数据等规则拒绝。
    """
    settings = get_settings()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationFailedError("仅支持 http/https 协议", code="agent_url_not_allowed")
    if parsed.scheme == "http" and settings.env == "prod":
        raise ValidationFailedError("生产环境仅允许 https 协议", code="agent_url_not_allowed")
    if parsed.username or parsed.password:
        raise ValidationFailedError("URL 中不允许携带用户名/密码", code="agent_url_not_allowed")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValidationFailedError("URL 缺少主机名", code="agent_url_not_allowed")
    if _host_in_allowlist(hostname, settings.a2a_host_allowlist):
        return url
    if settings.a2a_allow_loopback_resolved and _is_loopback_host(hostname):
        return url
    blocked, reason = is_private_host(hostname)
    if blocked:
        raise ValidationFailedError(f"URL 主机校验失败：{reason}", code="agent_url_not_allowed")
    return url


# --------------------------------------------------------------------------- 凭据加密


def _secret_key_bytes() -> bytes:
    key = get_settings().capability_secret_key.strip()
    if key:
        # 用 sha256 把任意长度配置密钥归一成 32 字节 AES-GCM 密钥。
        return hashlib.sha256(key.encode()).digest()
    return b""


def encrypt_secret(plaintext: str) -> str | None:
    """加密敏感值；返回 ``v1:<base64(nonce+ciphertext)>`` 或 ``None``（无密钥）。"""
    key = _secret_key_bytes()
    if not key:
        return None
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return "v1:" + base64.urlsafe_b64encode(nonce + ciphertext).decode()


def decrypt_secret(token: str) -> str | None:
    """解密敏感值；密文格式非法或密钥缺失返回 ``None``。"""
    if not token.startswith("v1:"):
        return None
    key = _secret_key_bytes()
    if not key:
        return None
    try:
        raw = base64.urlsafe_b64decode(token[3:])
        return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode()
    except Exception:
        logger.warning("凭据密文解密失败（密钥轮换？）")
        return None


def normalize_headers(raw: dict[str, str]) -> tuple[dict[str, object], str]:
    """把用户提交的请求头转成可落库的形态。

    - 普通头：明文存储；
    - 敏感头且值为 ``env:NAME``：存 ``{"secret_ref": "env:NAME"}``；
    - 敏感头明文：有 CAPABILITY_SECRET_KEY 时 AES-GCM 加密存储，否则 503。
    返回 ``(存储形态, credentials_hint)``。
    """
    stored: dict[str, object] = {}
    hints: list[str] = []
    for name, value in raw.items():
        value = (value or "").strip()
        if not value:
            continue
        if name.strip().lower() in _SECRET_HEADER_NAMES:
            if _SECRET_REF_PATTERN.match(value):
                stored[name] = {"secret_ref": value}
                hints.append(f"{name}: {value}")
            else:
                ciphertext = encrypt_secret(value)
                if ciphertext is None:
                    raise ServiceUnavailableError(
                        "保存凭据需要配置 CAPABILITY_SECRET_KEY，或改用 env:环境变量名 引用"
                    )
                stored[name] = {"ciphertext": ciphertext}
                hints.append(f"{name}: ****{value[-4:]}")
        else:
            stored[name] = value[:2000]
    return stored, " ".join(hints)[:160]


def resolve_headers(stored: dict[str, object]) -> dict[str, str]:
    """把落库形态还原成请求头；解析失败的 secret_ref 跳过（不发错误头）。"""
    resolved: dict[str, str] = {}
    for name, value in stored.items():
        if isinstance(value, str):
            resolved[name] = value
            continue
        if not isinstance(value, dict):
            continue
        ref = value.get("secret_ref")
        if isinstance(ref, str) and ref.startswith("env:"):
            env_value = os.environ.get(ref[4:], "")
            if env_value:
                resolved[name] = env_value
            continue
        ciphertext = value.get("ciphertext")
        if isinstance(ciphertext, str):
            plaintext = decrypt_secret(ciphertext)
            if plaintext:
                resolved[name] = plaintext
    return resolved


def credentials_hint_from(headers: dict[str, object]) -> str:
    """从存储形态生成脱敏提示（不回显明文）。"""
    hints: list[str] = []
    for name, value in headers.items():
        if isinstance(value, str):
            hints.append(name)
        elif isinstance(value, dict):
            if value.get("secret_ref"):
                hints.append(f"{name}: {value['secret_ref']}")
            elif value.get("ciphertext"):
                hints.append(f"{name}: ****")
    return " ".join(hints)[:160]
