"""Agent Card 拉取与校验（A2A 0.3 / 1.0 兼容）。

- 拉取 ``{url}/.well-known/agent-card.json``（兼容 ``agent.json``）；
- SSRF 校验、同源校验、协议版本白名单、技能数量/描述长度校验；
- 校验失败抛 ``ValidationFailedError``（``agent_card_invalid`` / ``agent_url_not_allowed``）；
- 重定向只允许同源跳转，最多 3 次；响应体上限受控。
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import get_settings
from app.core.errors import ValidationFailedError
from app.core.ssrf import validate_a2a_url
from app.modules.a2a.client import _test_transport_for

logger = logging.getLogger(__name__)

_WELL_KNOWN_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")
_MAX_REDIRECTS = 3
_SUPPORTED_PROTOCOL_PREFIXES = ("1.", "0.3")


@dataclass
class CardFetchResult:
    """一次 Card 拉取与规范化的结果。"""

    card: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    latency_ms: int = 0


def validate_card_url_same_origin(registered_url: str, card_url: str) -> None:
    """Card 中声明的 url 必须与注册 URL 同源（协议 + 主机 + 端口）。"""
    base = urlparse(registered_url)
    target = urlparse(card_url)
    if (target.scheme or "").lower() != (base.scheme or "").lower():
        raise ValidationFailedError("Agent Card 声明的 URL 与注册地址不同源", code="agent_card_invalid")
    if (target.hostname or "").lower() != (base.hostname or "").lower():
        raise ValidationFailedError("Agent Card 声明的 URL 与注册地址不同源", code="agent_card_invalid")
    if (target.port or (443 if target.scheme == "https" else 80)) != (
        base.port or (443 if base.scheme == "https" else 80)
    ):
        raise ValidationFailedError("Agent Card 声明的 URL 与注册地址不同源", code="agent_card_invalid")


def _validate_protocol_version(version: Any) -> str:
    raw = str(version or "").strip()
    if not raw:
        raise ValidationFailedError("Agent Card 缺少 protocolVersion", code="agent_card_invalid")
    if not any(raw.startswith(prefix) for prefix in _SUPPORTED_PROTOCOL_PREFIXES):
        raise ValidationFailedError(
            f"不支持的 A2A 协议版本：{raw}（支持 1.x 与 0.3.x）", code="agent_card_invalid"
        )
    return raw[:16]


def validate_card_schema(card: dict[str, Any]) -> None:
    """用 ``a2a-sdk`` 的 AgentCard 模型做一次规范校验。

    SDK 的 ``parse_agent_card`` 会原地修改 dict（兼容 0.3 字段），因此传入副本。
    业务层仍保留同源、协议版本、技能数量等自定义校验。
    """
    from a2a.client.card_resolver import parse_agent_card

    try:
        parse_agent_card(copy.deepcopy(card))
    except Exception as exc:
        raise ValidationFailedError(
            "Agent Card 结构不符合 A2A 规范", code="agent_card_invalid"
        ) from exc


def normalize_card(card: dict[str, Any], *, registered_url: str) -> CardFetchResult:
    """把原始 Card 校验并规范化；返回 preview 需要的结构。

    技能描述超长截断并记 warning；技能数量超限直接拒绝。
    """
    settings = get_settings()
    result = CardFetchResult(card=card)
    if not isinstance(card, dict):
        raise ValidationFailedError("Agent Card 不是 JSON 对象", code="agent_card_invalid")
    validate_card_schema(card)

    name = str(card.get("name") or "").strip()
    if not name:
        raise ValidationFailedError("Agent Card 缺少 name", code="agent_card_invalid")
    description = str(card.get("description") or "").strip()

    card_url = str(card.get("url") or "").strip()
    if not card_url:
        raise ValidationFailedError("Agent Card 缺少 url", code="agent_card_invalid")
    validate_card_url_same_origin(registered_url, card_url)

    protocol_version = _validate_protocol_version(card.get("protocolVersion"))

    raw_skills = card.get("skills") or []
    if not isinstance(raw_skills, list) or len(raw_skills) > settings.a2a_max_skills:
        raise ValidationFailedError(
            f"Agent Card 技能数量不合法（0-{settings.a2a_max_skills}）", code="agent_card_invalid"
        )
    skills: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in raw_skills:
        if not isinstance(item, dict):
            raise ValidationFailedError("Agent Card 技能格式非法", code="agent_card_invalid")
        skill_id = str(item.get("id") or "").strip()
        skill_name = str(item.get("name") or "").strip()
        if not skill_id or not skill_name:
            raise ValidationFailedError("Agent Card 技能缺少 id/name", code="agent_card_invalid")
        if skill_id in seen_ids:
            raise ValidationFailedError(
                f"Agent Card 技能 ID 重复：{skill_id}", code="agent_card_invalid"
            )
        seen_ids.add(skill_id)
        skill_description = str(item.get("description") or "").strip()
        if len(skill_description) > settings.a2a_skill_description_max_chars:
            result.warnings.append(f"技能 {skill_id} 描述超长已截断")
            skill_description = skill_description[: settings.a2a_skill_description_max_chars]
        skills.append(
            {
                "id": skill_id[:64],
                "name": skill_name[:128],
                "description": skill_description,
                "tags": [str(tag)[:32] for tag in item.get("tags", [])][:10],
                "examples": [str(example)[:200] for example in item.get("examples", [])][:5],
            }
        )

    raw_capabilities = card.get("capabilities") or {}
    capabilities = {
        "streaming": bool(raw_capabilities.get("streaming", False)),
        "push_notifications": bool(raw_capabilities.get("pushNotifications", False)),
    }

    result.normalized = {
        "name": name[:128],
        "description": description[:2000],
        "url": card_url,
        "protocol_version": protocol_version,
        "skills": skills,
        "capabilities": capabilities,
    }
    return result


def _same_origin(base_url: str, target_url: str) -> bool:
    base = urlparse(base_url)
    target = urlparse(target_url)
    base_port = base.port or (443 if base.scheme == "https" else 80)
    target_port = target.port or (443 if target.scheme == "https" else 80)
    return (
        (target.scheme or "").lower() == (base.scheme or "").lower()
        and (target.hostname or "").lower() == (base.hostname or "").lower()
        and target_port == base_port
    )


async def fetch_agent_card(
    url: str, *, headers: dict[str, str] | None = None
) -> CardFetchResult:
    """拉取并规范化 Agent Card；不落库。

    ``url`` 为基础地址（如 ``http://127.0.0.1:9100``），Card 从 well-known 路径读取；
    失败抛 ``ValidationFailedError``（agent_url_not_allowed / agent_card_invalid）。
    """
    settings = get_settings()
    validate_a2a_url(url)
    base = url.rstrip("/")
    headers = dict(headers or {})
    headers.setdefault("Accept", "application/json")
    started = time.monotonic()

    transport = _test_transport_for(base)
    async with httpx.AsyncClient(
        timeout=settings.a2a_card_timeout_seconds,
        follow_redirects=False,
        transport=transport,
        trust_env=False,
    ) as client:
        current = base
        last_status = 0
        try:
            for _ in range(_MAX_REDIRECTS + 1):
                for path in _WELL_KNOWN_PATHS:
                    target = current + path
                    try:
                        response = await client.get(target, headers=headers)
                    except httpx.TimeoutException as exc:
                        raise ValidationFailedError(
                            "Agent Card 拉取超时", code="agent_card_invalid"
                        ) from exc
                    except (httpx.InvalidURL, httpx.HTTPError) as exc:
                        raise ValidationFailedError(
                            f"Agent Card 拉取失败：{exc.__class__.__name__}",
                            code="agent_card_invalid",
                        ) from exc
                    last_status = response.status_code
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location") or ""
                        if not location:
                            raise ValidationFailedError(
                                "Agent Card 重定向缺少 Location", code="agent_card_invalid"
                            )
                        target_url = urljoin(target, location)
                        if not _same_origin(base, target_url):
                            raise ValidationFailedError(
                                "Agent Card 禁止跨源重定向", code="agent_card_invalid"
                            )
                        current = target_url.rstrip("/")
                        break
                    if response.status_code == 404:
                        continue
                    if response.status_code != 200:
                        raise ValidationFailedError(
                            f"Agent Card 拉取失败（HTTP {response.status_code}）",
                            code="agent_card_invalid",
                        )
                    if len(response.content) > settings.a2a_max_response_bytes:
                        raise ValidationFailedError(
                            "Agent Card 响应体过大", code="agent_card_invalid"
                        )
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise ValidationFailedError(
                            "Agent Card 不是合法 JSON", code="agent_card_invalid"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ValidationFailedError(
                            "Agent Card 不是 JSON 对象", code="agent_card_invalid"
                        )
                    result = normalize_card(payload, registered_url=base)
                    result.latency_ms = int((time.monotonic() - started) * 1000)
                    return result
                else:
                    raise ValidationFailedError(
                        "未找到 Agent Card（.well-known/agent-card.json 与 agent.json 均 404）",
                        code="agent_card_invalid",
                    )
        finally:
            logger.info("agent card fetch url=%s status=%s", base, last_status)
    raise ValidationFailedError(
        "Agent Card 重定向次数过多", code="agent_card_invalid"
    )
