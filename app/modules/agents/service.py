"""远程 Agent 注册表业务：Card 拉取、CRUD、刷新与健康检查。

- v1 全局注册表：所有登录用户可读、可写（写操作落审计）；
- 注册前先拉取并校验 Agent Card（SSRF / 同源 / 协议版本 / 技能校验）；
- 健康阈值：成功且延迟 <2s → healthy，≥2s → degraded；连续失败 ≥3 → unhealthy；
- 刷新失败保留旧 Card 快照并标记 unhealthy；软删保留历史任务。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_audit
from app.core.config import get_settings
from app.core.cursor import decode_cursor, encode_cursor
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.ssrf import resolve_headers
from app.db.models.remote import A2A_ACTIVE_STATES, A2aTask, RemoteAgent
from app.db.models.user import User
from app.modules.a2a import A2aClient, A2aError, fetch_agent_card
from app.modules.agents.schemas import (
    AgentCreateRequest,
    AgentDetailOut,
    AgentHealthResult,
    AgentOut,
    AgentPage,
    AgentPreviewRequest,
    AgentPreviewResult,
    AgentRecentTask,
    AgentRefreshResult,
    AgentStats,
    AgentUpdateRequest,
)

logger = logging.getLogger(__name__)

_HEALTHY_LATENCY_MS = 2000
_UNHEALTHY_FAILURES = 3
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _now() -> datetime:
    return datetime.now(UTC)


def _slugify(name: str) -> str:
    raw = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    raw = re.sub(r"-{2,}", "-", raw)[:63].strip("-")
    return raw or "agent"


def _duration_ms(task: A2aTask) -> int | None:
    if task.finished_at and task.started_at:
        return int((task.finished_at - task.started_at).total_seconds() * 1000)
    if task.started_at:
        return int((_now() - task.started_at).total_seconds() * 1000)
    return None


# --------------------------------------------------------------------------- 输出组装


def _skill_refs(agent: RemoteAgent) -> list[dict]:
    refs: list[dict] = []
    for skill in agent.skills or []:
        if not isinstance(skill, dict):
            continue
        refs.append(
            {
                "id": str(skill.get("id") or "")[:64],
                "name": str(skill.get("name") or "")[:128],
                "description": str(skill.get("description") or ""),
            }
        )
    return refs


def _recent_task_out(task: A2aTask) -> AgentRecentTask:
    return AgentRecentTask(
        id=task.id,
        a2a_task_id=task.a2a_task_id,
        state=task.state,
        duration_ms=_duration_ms(task),
        finished_at=task.finished_at,
    )


def _agent_out(
    agent: RemoteAgent, *, recent_task: A2aTask | None = None, recent_tasks: list[A2aTask] | None = None
) -> AgentOut:
    capabilities = agent.capabilities or {}
    common = dict(
        id=agent.id,
        slug=agent.slug,
        name=agent.name,
        description=agent.description or "",
        url=agent.url,
        protocol_version=agent.protocol_version or "",
        skills=_skill_refs(agent),
        skill_count=len(_skill_refs(agent)),
        auth_type=agent.auth_type,
        auth_scheme=agent.auth_scheme or "none",
        auth_hint=agent.auth_hint or "",
        capabilities={
            "streaming": bool(capabilities.get("streaming")),
            "push_notifications": bool(capabilities.get("push_notifications")),
        },
        health_status=agent.health_status,
        enabled=agent.enabled,
        last_checked_at=agent.last_checked_at,
        last_latency_ms=agent.last_latency_ms,
        last_error=agent.last_error,
        recent_task=_recent_task_out(recent_task) if recent_task else None,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )
    if recent_tasks is not None:
        return AgentDetailOut(
            **common,
            card=agent.card or None,
            recent_tasks=[_recent_task_out(item) for item in recent_tasks],
            requires_approval=agent.requires_approval,
        )
    return AgentOut(**common)


# --------------------------------------------------------------------------- Card 与鉴权


async def preview_agent(payload: AgentPreviewRequest) -> AgentPreviewResult:
    """拉取并校验 Agent Card；不落库、不生成工具。"""
    result = await fetch_agent_card(payload.url)
    normalized = result.normalized
    return AgentPreviewResult(
        valid=True,
        card=result.card,
        normalized={
            "name": normalized["name"],
            "description": normalized["description"],
            "url": normalized["url"],
            "protocol_version": normalized["protocol_version"],
            "skills": [
                {"id": item["id"], "name": item["name"], "description": item["description"]}
                for item in normalized["skills"]
            ],
            "capabilities": normalized["capabilities"],
        },
        warnings=result.warnings,
    )


def _infer_auth_scheme(card: dict) -> str:
    """从 Card securitySchemes 推断展示用 scheme：bearer / header / oauth2 / none。"""
    schemes = card.get("securitySchemes") or {}
    if not isinstance(schemes, dict):
        return "none"
    for spec in schemes.values():
        if not isinstance(spec, dict):
            continue
        scheme_type = str(spec.get("type") or "").lower()
        if scheme_type == "http" and str(spec.get("scheme") or "").lower() == "bearer":
            return "bearer"
        if scheme_type in {"apikey", "api_key"}:
            return "header"
        if "oauth" in scheme_type:
            return "oauth2"
    return "none"


def _requires_approval(card: dict) -> bool:
    for skill in card.get("skills") or []:
        if isinstance(skill, dict):
            metadata = skill.get("metadata")
            if isinstance(metadata, dict) and metadata.get("requiresApproval"):
                return True
    return False


def _apply_auth(
    agent: RemoteAgent,
    card: dict,
    auth: object | None,
) -> None:
    """把注册/更新时的 auth 输入落到 Agent；secret_ref 只存引用。"""
    if auth is None:
        return
    auth_type = getattr(auth, "type", None) or "none"
    secret_ref = (getattr(auth, "secret_ref", None) or "").strip() or None
    if auth_type == "none":
        agent.auth_type = "none"
        agent.auth_scheme = "none"
        agent.secret_ref = None
        return
    if auth_type == "oauth2":
        agent.auth_type = "oauth2"
        agent.auth_scheme = "oauth2"
        agent.secret_ref = None
        return
    # api_key：scheme 优先从 Card 推断，其次默认 bearer。
    agent.auth_type = "api_key"
    agent.auth_scheme = _infer_auth_scheme(card) if agent.auth_scheme == "none" else agent.auth_scheme
    if agent.auth_scheme in {"none", "oauth2"}:
        agent.auth_scheme = "bearer"
    agent.secret_ref = secret_ref
    if secret_ref:
        agent.auth_hint = secret_ref


def _resolved_headers(agent: RemoteAgent) -> dict[str, str]:
    """调用远程时使用的请求头（secret_ref / 密文还原）。"""
    headers = resolve_headers(agent.headers or {})
    if agent.secret_ref and agent.auth_type == "api_key":
        resolved = resolve_headers({"Authorization": {"secret_ref": agent.secret_ref}})
        if resolved:
            headers["Authorization"] = resolved["Authorization"]
    return headers


# --------------------------------------------------------------------------- 创建 / 查询


async def create_agent(
    db: AsyncSession, user: User, payload: AgentCreateRequest
) -> AgentDetailOut:
    settings = get_settings()
    # 显式 slug 时先做幂等/冲突检查，避免对冲突 URL 发起不必要的 Card 拉取。
    if payload.slug:
        existing = await db.scalar(
            select(RemoteAgent).where(
                RemoteAgent.slug == payload.slug, RemoteAgent.deleted_at.is_(None)
            )
        )
        if existing is not None:
            if existing.url == payload.url:
                return await _detail_with_recent(db, existing)
            raise ConflictError(f"slug「{payload.slug}」已被其他 Agent 使用", code="agent_slug_conflict")

    fetched = await fetch_agent_card(payload.url)
    normalized = fetched.normalized
    slug = payload.slug or _slugify(normalized["name"])

    existing = await db.scalar(
        select(RemoteAgent).where(RemoteAgent.slug == slug, RemoteAgent.deleted_at.is_(None))
    )
    if existing is not None:
        if existing.url == payload.url:
            return await _detail_with_recent(db, existing)
        raise ConflictError(
            f"slug「{slug}」已被其他 Agent 使用", code="agent_slug_conflict"
        )

    agent = RemoteAgent(
        owner_user_id=user.id,
        slug=slug,
        name=(payload.name or normalized["name"])[:128],
        description=normalized["description"][: settings.a2a_skill_description_max_chars],
        url=payload.url,
        protocol_version=normalized["protocol_version"],
        card=fetched.card,
        skills=normalized["skills"],
        capabilities=normalized["capabilities"],
        auth_type="none",
        auth_scheme="none",
        auth_hint="",
        secret_ref=None,
        headers={},
        requires_approval=_requires_approval(fetched.card),
        enabled=True,
        health_status="unknown",
        consecutive_failures=0,
        name_edited=bool(payload.name),
    )
    _apply_auth(agent, fetched.card, payload.auth)
    db.add(agent)
    await db.flush()
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="agent.created",
        resource_type="agent",
        resource_id=str(agent.id),
        detail={"slug": agent.slug, "url": agent.url},
    )
    await db.commit()
    await db.refresh(agent)
    return await _detail_with_recent(db, agent)


async def _detail_with_recent(db: AsyncSession, agent: RemoteAgent) -> AgentDetailOut:
    recent = list(
        (
            await db.execute(
                select(A2aTask)
                .where(A2aTask.remote_agent_id == agent.id)
                .order_by(A2aTask.created_at.desc())
                .limit(3)
            )
        ).scalars()
    )
    return _agent_out(agent, recent_tasks=recent)


async def _get_agent(db: AsyncSession, agent_id: uuid.UUID) -> RemoteAgent:
    agent = await db.scalar(
        select(RemoteAgent).where(RemoteAgent.id == agent_id, RemoteAgent.deleted_at.is_(None))
    )
    if agent is None:
        raise NotFoundError("远程 Agent 不存在")
    return agent


async def get_agent(db: AsyncSession, agent_id: uuid.UUID) -> AgentDetailOut:
    agent = await _get_agent(db, agent_id)
    return await _detail_with_recent(db, agent)


async def list_agents(
    db: AsyncSession,
    *,
    health_status: str | None,
    enabled: bool | None,
    q: str | None,
    limit: int,
    cursor: str | None,
) -> AgentPage:
    filters = [RemoteAgent.deleted_at.is_(None)]
    if health_status:
        filters.append(RemoteAgent.health_status == health_status)
    if enabled is not None:
        filters.append(RemoteAgent.enabled == enabled)
    if q:
        pattern = f"%{q}%"
        filters.append(
            or_(
                RemoteAgent.name.ilike(pattern),
                RemoteAgent.slug.ilike(pattern),
                RemoteAgent.description.ilike(pattern),
            )
        )

    decoded = decode_cursor(cursor)
    if decoded.get("created_at") and decoded.get("id"):
        from datetime import datetime as dt

        try:
            since = dt.fromisoformat(str(decoded["created_at"]))
            filters.append(
                or_(
                    RemoteAgent.created_at < since,
                    and_(RemoteAgent.created_at == since, RemoteAgent.id < uuid.UUID(str(decoded["id"]))),
                )
            )
        except ValueError as exc:
            raise ValidationFailedError("分页游标无效") from exc

    stmt = (
        select(RemoteAgent)
        .where(*filters)
        .order_by(RemoteAgent.created_at.desc(), RemoteAgent.id.desc())
        .limit(limit + 1)
    )
    rows = list((await db.execute(stmt)).scalars())
    has_more = len(rows) > limit
    rows = rows[:limit]

    recent = await _latest_tasks_map(db, [item.id for item in rows])
    items = [_agent_out(item, recent_task=recent.get(item.id)) for item in rows]
    stats = await _agent_stats(db, filters)
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor({"created_at": last.created_at, "id": last.id})
    return AgentPage(items=items, next_cursor=next_cursor, has_more=has_more, stats=stats)


async def _latest_tasks_map(db: AsyncSession, agent_ids: list[uuid.UUID]) -> dict[uuid.UUID, A2aTask]:
    if not agent_ids:
        return {}
    latest = (
        select(
            A2aTask.remote_agent_id,
            func.max(A2aTask.created_at).label("max_created"),
        )
        .where(A2aTask.remote_agent_id.in_(agent_ids))
        .group_by(A2aTask.remote_agent_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(A2aTask)
            .join(
                latest,
                and_(
                    A2aTask.remote_agent_id == latest.c.remote_agent_id,
                    A2aTask.created_at == latest.c.max_created,
                ),
            )
            .order_by(A2aTask.created_at.desc())
            .limit(len(agent_ids))
        )
    ).scalars()
    result: dict[uuid.UUID, A2aTask] = {}
    for task in rows:
        result.setdefault(task.remote_agent_id, task)
    return result


async def _agent_stats(db: AsyncSession, filters: list) -> AgentStats:
    base = [*filters]
    stats = AgentStats()
    for key, value in {
        "total": None,
        "healthy": "healthy",
        "degraded": "degraded",
        "unhealthy": "unhealthy",
    }.items():
        extra = [RemoteAgent.health_status == value] if value else []
        count = await db.scalar(select(func.count()).select_from(RemoteAgent).where(*base, *extra))
        setattr(stats, key, int(count or 0))
    stats.enabled = int(
        await db.scalar(
            select(func.count()).select_from(RemoteAgent).where(*base, RemoteAgent.enabled.is_(True))
        )
        or 0
    )
    stats.disabled = int(
        await db.scalar(
            select(func.count()).select_from(RemoteAgent).where(*base, RemoteAgent.enabled.is_(False))
        )
        or 0
    )
    return stats


# --------------------------------------------------------------------------- 更新 / 删除


async def update_agent(
    db: AsyncSession, user: User, agent_id: uuid.UUID, payload: AgentUpdateRequest
) -> AgentDetailOut:
    agent = await _get_agent(db, agent_id)
    changed: list[str] = []
    if payload.name is not None and payload.name != agent.name:
        agent.name = payload.name[:128]
        agent.name_edited = True
        changed.append("name")
    if payload.enabled is not None and payload.enabled != agent.enabled:
        agent.enabled = payload.enabled
        changed.append("enabled")
    if payload.auth is not None:
        _apply_auth(agent, agent.card or {}, payload.auth)
        changed.append("auth")
    if payload.headers is not None:
        from app.core.ssrf import normalize_headers

        stored, hint = normalize_headers(payload.headers)
        agent.headers = stored
        if hint:
            agent.auth_hint = hint
        changed.append("headers")
    if changed:
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="agent.updated",
            resource_type="agent",
            resource_id=str(agent.id),
            detail={"fields": changed},
        )
        await db.commit()
        await db.refresh(agent)
    return await _detail_with_recent(db, agent)


async def delete_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> None:
    agent = await _get_agent(db, agent_id)
    active = await db.scalar(
        select(func.count())
        .select_from(A2aTask)
        .where(A2aTask.remote_agent_id == agent_id, A2aTask.state.in_(A2A_ACTIVE_STATES))
    )
    if active:
        raise ConflictError("该 Agent 存在运行中的任务，无法删除", code="agent_has_active_tasks")
    agent.deleted_at = _now()
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="agent.deleted",
        resource_type="agent",
        resource_id=str(agent.id),
        detail={"slug": agent.slug},
    )
    await db.commit()


# --------------------------------------------------------------------------- 刷新 / 健康


async def _record_health(
    db: AsyncSession, agent: RemoteAgent, *, ok: bool, latency_ms: int | None, error: str | None
) -> None:
    now = _now()
    agent.last_checked_at = now
    agent.last_latency_ms = latency_ms
    if ok:
        agent.consecutive_failures = 0
        agent.health_status = "healthy" if (latency_ms or 0) < _HEALTHY_LATENCY_MS else "degraded"
        agent.last_error = None
    else:
        agent.consecutive_failures += 1
        agent.health_status = (
            "unhealthy" if agent.consecutive_failures >= _UNHEALTHY_FAILURES else "degraded"
        )
        agent.last_error = (error or "健康检查失败")[:500]


async def refresh_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> AgentRefreshResult:
    agent = await _get_agent(db, agent_id)
    try:
        fetched = await fetch_agent_card(agent.url, headers=_resolved_headers(agent))
    except ValidationFailedError as exc:
        await _record_health(db, agent, ok=False, latency_ms=None, error=exc.message)
        await write_audit(
            db,
            actor_type="user",
            actor_id=str(user.id),
            action="agent.refresh_failed",
            resource_type="agent",
            resource_id=str(agent.id),
            detail={"error": exc.message[:200]},
        )
        await db.commit()
        raise
    normalized = fetched.normalized
    changed_fields: list[str] = []
    if (agent.card or {}) != fetched.card:
        changed_fields.append("card")
        agent.card = fetched.card
    old_skills = agent.skills or []
    new_skills = normalized["skills"]
    if old_skills != new_skills:
        changed_fields.append("skills")
        agent.skills = new_skills
    if agent.description != normalized["description"]:
        changed_fields.append("description")
        agent.description = normalized["description"]
    if agent.protocol_version != normalized["protocol_version"]:
        changed_fields.append("protocol_version")
        agent.protocol_version = normalized["protocol_version"]
    if (agent.capabilities or {}) != normalized["capabilities"]:
        changed_fields.append("capabilities")
        agent.capabilities = normalized["capabilities"]
    if not agent.name_edited and agent.name != normalized["name"]:
        changed_fields.append("name")
        agent.name = normalized["name"]
    await _record_health(db, agent, ok=True, latency_ms=fetched.latency_ms, error=None)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="agent.refreshed",
        resource_type="agent",
        resource_id=str(agent.id),
        detail={"changed_fields": changed_fields},
    )
    await db.commit()
    await db.refresh(agent)
    return AgentRefreshResult(
        agent=await _detail_with_recent(db, agent),
        changed=bool(changed_fields),
        changed_fields=changed_fields,
    )


async def health_check_agent(db: AsyncSession, user: User, agent_id: uuid.UUID) -> AgentHealthResult:
    agent = await _get_agent(db, agent_id)
    error: str | None = None
    latency_ms: int | None = None
    try:
        fetched = await fetch_agent_card(agent.url, headers=_resolved_headers(agent))
        latency_ms = fetched.latency_ms
        probe = A2aClient(agent.url, headers=_resolved_headers(agent))
        try:
            await probe.get_task("__agent_hub_health_probe__")
        except A2aError as exc:
            if exc.code == "a2a_remote_error":
                pass  # 任务不存在：服务可达，视为探测成功。
            else:
                raise
        ok = True
    except ValidationFailedError as exc:
        error = exc.message
        ok = False
    except A2aError as exc:
        error = exc.message
        ok = False
    await _record_health(db, agent, ok=ok, latency_ms=latency_ms, error=error)
    await write_audit(
        db,
        actor_type="user",
        actor_id=str(user.id),
        action="agent.health_check",
        resource_type="agent",
        resource_id=str(agent.id),
        detail={"ok": ok, "latency_ms": latency_ms},
    )
    await db.commit()
    await db.refresh(agent)
    return AgentHealthResult(
        health_status=agent.health_status,
        latency_ms=agent.last_latency_ms,
        checked_at=agent.last_checked_at or _now(),
        error=agent.last_error,
    )


# --------------------------------------------------------------------------- 聊天目录


async def enabled_agents_for_chat(db: AsyncSession) -> list[RemoteAgent]:
    """聊天工具目录：enabled、未删除且健康状态可用（非 unhealthy）。"""
    rows = (
        await db.execute(
            select(RemoteAgent)
            .where(
                RemoteAgent.deleted_at.is_(None),
                RemoteAgent.enabled.is_(True),
                RemoteAgent.health_status != "unhealthy",
            )
            .order_by(RemoteAgent.slug.asc())
        )
    ).scalars()
    return list(rows)
