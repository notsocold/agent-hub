"""审批 gate 节点：创建审批、``interrupt()`` 挂起、取消时清理。

设计要点（见 M5 spec §7）：
- gate 独立成节点，恢复重放不会重跑 tools；
- 幂等键 ``(run_id, tool_call_id)``，重复执行复用同一条审批；
- 一轮多审批只调用一次 ``interrupt()``，全部决定后才恢复；
- 取消/过期时返回 ``halted=True`` 并给本轮全部 tool_calls 补「已取消」ToolMessage。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.types import interrupt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.agents.graph import GraphState
from app.core.config import get_settings
from app.db.models.approval import Approval
from app.db.models.chat import Run
from app.db.models.mcp import McpServer
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

# 撤销/取消时补进历史的消息：让模型知道工具没有执行，线程也能继续使用。
HALT_MESSAGE = "用户已取消本次工具调用，操作未执行。"
# 审批超时后 gate 发现过期时的提示。
EXPIRED_MESSAGE = "审批已超时，本次工具调用未执行。"

_RISKS = {"read", "write", "destructive", "unknown"}


@dataclass(frozen=True)
class ApprovalSpec:
    """gate 判断某次 tool_call 需要审批时携带的元数据。"""

    tool_name: str
    display_name: str
    risk: str
    source: str
    executor: str
    target: str = ""
    server_id: uuid.UUID | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def _compact(value: Any, limit: int = 500) -> str:
    """把任意值压成不超过 ``limit`` 字符的字符串（截断时以省略号结尾）。"""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return raw
    return raw[: max(0, limit - 1)] + "…"


def _side_effect(risk: str) -> str:
    return {
        "read": "只读操作，无外部副作用",
        "write": "写操作：会修改外部系统数据",
        "destructive": "破坏性操作：删除/覆盖等不可逆变更",
        "unknown": "操作副作用未知",
    }.get(risk, "操作副作用未知")


def _halt_update(state: dict[str, Any], message: str = HALT_MESSAGE) -> dict[str, Any]:
    """给本轮全部 tool_calls 补 ToolMessage，并路由 END。"""
    last = state["messages"][-1]
    calls = list(getattr(last, "tool_calls", None) or [])
    return {
        "halted": True,
        "messages": [
            ToolMessage(content=message, tool_call_id=str(call.get("id") or "")) for call in calls
        ],
    }


def build_halt_gate() -> Callable[[GraphState], Awaitable[dict[str, Any]]]:
    """清理专用 gate：不查库、不建审批，收到 resume 就 halt 并补消息。"""

    async def halt_gate(state: GraphState) -> dict[str, Any]:
        return _halt_update(state)

    return halt_gate


async def _find_approval(db, run_id: uuid.UUID, tool_call_id: str) -> Approval | None:
    return await db.scalar(
        select(Approval)
        .where(Approval.run_id == run_id, Approval.tool_call_id == tool_call_id)
        .execution_options(populate_existing=True)
    )


async def _load_approvals(db, run_id: uuid.UUID, tool_call_ids: list[str]) -> list[Approval]:
    if not tool_call_ids:
        return []
    rows = (
        await db.execute(
            select(Approval)
            .where(Approval.run_id == run_id, Approval.tool_call_id.in_(tool_call_ids))
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return list(rows)


async def _cancel_run_approvals(db, run_id: uuid.UUID) -> None:
    """取消尚未执行的审批（pending 与已批准但未执行）。"""
    await db.execute(
        update(Approval)
        .where(
            Approval.run_id == run_id,
            Approval.status.in_(("pending", "approved")),
        )
        .values(status="canceled")
    )
    await db.commit()


async def _ensure_approval(db, runner: Any, call: dict[str, Any], spec: ApprovalSpec) -> None:
    """幂等创建审批；只有首次创建时才发 ``approval.required``。"""
    tool_call_id = str(call.get("id") or "")
    if not tool_call_id:
        # 模型没给 id 的异常情况：跳过审批（工具执行侧也会生成临时 id，无法对齐）。
        logger.warning("tool_call 缺少 id，跳过审批创建 tool=%s", spec.tool_name)
        return
    if await _find_approval(db, runner.run_id, tool_call_id) is not None:
        return
    target = spec.target
    if spec.server_id is not None:
        server = await db.get(McpServer, spec.server_id)
        if server is not None:
            target = server.url or ""
    args = call.get("args") or {}
    preview = _compact(args, 500)
    settings = get_settings()
    risk = spec.risk if spec.risk in _RISKS else "unknown"
    approval = Approval(
        id=uuid.uuid4(),
        run_id=runner.run_id,
        tool_call_id=tool_call_id,
        tool_name=spec.tool_name,
        tool_display_name=spec.display_name,
        source=spec.source,
        executor=spec.executor,
        target=(target or "")[:2000],
        side_effect=_side_effect(risk),
        risk=risk,
        reason=f"工具「{spec.tool_name}」需要人工确认后执行",
        payload=runner.sanitize_args(args),
        payload_preview=preview,
        status="pending",
        expires_at=_now() + timedelta(seconds=settings.approval_timeout_seconds),
    )
    db.add(approval)
    try:
        await db.commit()
        await db.refresh(approval)
    except IntegrityError:
        await db.rollback()
        logger.info("审批已被并发创建，复用已有记录 tool_call_id=%s", tool_call_id)
        return
    await runner.publisher.emit(
        "approval.required",
        {
            "approval_id": str(approval.id),
            "tool_call_id": tool_call_id,
            "tool": spec.tool_name,
            "tool_display_name": approval.tool_display_name,
            "source": spec.source,
            "executor": approval.executor,
            "target": approval.target,
            "side_effect": approval.side_effect,
            "risk": approval.risk,
            "reason": approval.reason,
            "payload": approval.payload,
            "payload_preview": approval.payload_preview,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
        },
        event_type="approval",
        title="等待人工确认",
        subtitle=f"{spec.tool_name}：{preview}",
    )


def build_approval_gate(runner: Any) -> Callable[[GraphState], Awaitable[dict[str, Any]]]:
    """构造审批 gate；``runner`` 提供 ``approval_spec`` / publisher / sanitize_args。"""

    async def approval_gate(state: GraphState) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = list(getattr(last, "tool_calls", None) or [])
        pairs = [(call, runner.approval_spec(call)) for call in calls]
        pairs = [(call, spec) for call, spec in pairs if spec is not None]
        if not pairs:
            return {}
        tool_call_ids = [str(call.get("id") or "") for call, _ in pairs]

        async with get_session_factory()() as db:
            run = await db.get(Run, runner.run_id)
            if run is None or run.status == "canceled":
                await _cancel_run_approvals(db, runner.run_id)
                return _halt_update(state)
            for call, spec in pairs:
                await _ensure_approval(db, runner, call, spec)
            approvals = await _load_approvals(db, runner.run_id, tool_call_ids)

        logger.debug(
            "gate 状态 run_id=%s approvals=%s",
            runner.run_id,
            [(str(item.id), item.status) for item in approvals],
        )
        if not approvals or any(item.status in {"expired", "canceled"} for item in approvals):
            async with get_session_factory()() as db:
                await _cancel_run_approvals(db, runner.run_id)
            return _halt_update(
                state,
                EXPIRED_MESSAGE
                if any(item.status == "expired" for item in approvals)
                else HALT_MESSAGE,
            )
        if all(item.status in {"approved", "rejected"} for item in approvals):
            return {}

        # 一轮多审批只挂起一次；所有 pending 决定完，decide 才调度 resume。
        resume_value = interrupt(
            {
                "run_id": str(runner.run_id),
                "approvals": [
                    str(item.id) for item in approvals if item.status == "pending"
                ],
            }
        )
        logger.debug("审批 gate 恢复 run_id=%s resume=%s", runner.run_id, resume_value)

        # 恢复轮次以 DB 状态为准复查，避免 resume 值与审批错配。
        async with get_session_factory()() as db:
            refreshed = await _load_approvals(db, runner.run_id, tool_call_ids)
            if not refreshed or any(
                item.status in {"expired", "canceled"} for item in refreshed
            ):
                await _cancel_run_approvals(db, runner.run_id)
                return _halt_update(
                    state,
                    EXPIRED_MESSAGE
                    if any(item.status == "expired" for item in refreshed)
                    else HALT_MESSAGE,
                )
        return {}

    return approval_gate
