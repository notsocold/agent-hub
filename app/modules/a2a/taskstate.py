"""A2A 任务状态机共享辅助：timeline、状态转移、过期与可用动作。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db.models.remote import A2A_ACTIVE_STATES, A2aTask

_ACTIONS = {
    "submitted": ["cancel"],
    "working": ["cancel"],
    "input_required": ["resume", "cancel"],
    "auth_required": ["authorize", "reject", "cancel"],
    "completed": [],
    "failed": [],
    "canceled": [],
    "rejected": [],
}

_WAITING_STATES = ("input_required", "auth_required")


def _now() -> datetime:
    return datetime.now(UTC)


def available_actions(state: str) -> list[str]:
    """由 state 推导后端可执行动作。"""
    return _ACTIONS.get(state, [])


def append_timeline(task: A2aTask, state: str, *, note: str = "") -> None:
    """追加状态转移：给上一条填停留时长，新条 duration_ms 暂为 None。"""
    now = _now()
    timeline = [dict(item) for item in (task.timeline or []) if isinstance(item, dict)]
    if timeline:
        last_at = timeline[-1].get("at")
        try:
            since = datetime.fromisoformat(str(last_at)) if last_at else None
        except ValueError:
            since = None
        if since is not None and timeline[-1].get("duration_ms") is None:
            timeline[-1]["duration_ms"] = max(0, int((now - since).total_seconds() * 1000))
    timeline.append(
        {
            "state": state,
            "at": now.isoformat(),
            "duration_ms": None,
            "note": note[:500],
        }
    )
    task.timeline = timeline
    task.state = state
    if state not in A2A_ACTIVE_STATES and task.finished_at is None:
        task.finished_at = now


def waiting_request(task: A2aTask) -> dict[str, Any] | None:
    """当前等待状态对应的请求负载（input_request / auth_request）。"""
    if task.state == "input_required":
        return task.input_request
    if task.state == "auth_required":
        return task.auth_request
    return None


def expires_at_of(task: A2aTask) -> datetime | None:
    payload = waiting_request(task)
    if not payload:
        return None
    raw = payload.get("expires_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def is_expired(task: A2aTask) -> bool:
    if task.state not in _WAITING_STATES:
        return False
    expires = expires_at_of(task)
    return expires is not None and expires <= _now()


def expire_waiting(task: A2aTask) -> bool:
    """等待超时：置 canceled 并追加 timeline；返回是否有变化。"""
    if not is_expired(task):
        return False
    append_timeline(task, "canceled", note="等待超时")
    return True


def wake_decision_for(action: str, task: A2aTask, extra: dict[str, Any] | None = None) -> None:
    """把动作决定投递给运行中的工具等待者（进程内 registry）。"""
    if task.run_id is None:
        return
    from app.modules.chat.registry import registry

    context = registry.get(task.run_id)
    if context is None:
        return
    context.resolve_a2a_task(task.id, {"action": action, **(extra or {})})


def task_duration_ms(task: A2aTask) -> int | None:
    if task.finished_at and task.started_at:
        return max(0, int((task.finished_at - task.started_at).total_seconds() * 1000))
    if task.started_at:
        return max(0, int((_now() - task.started_at).total_seconds() * 1000))
    return None


def task_error_summary(task: A2aTask) -> str | None:
    error = task.error or {}
    message = str(error.get("message") or "").strip()
    return message[:200] if message else None


def wake_for_cancel(task: A2aTask) -> None:
    wake_decision_for("cancel", task)


__all__ = [
    "append_timeline",
    "available_actions",
    "expire_waiting",
    "expires_at_of",
    "is_expired",
    "task_duration_ms",
    "task_error_summary",
    "wake_decision_for",
    "wake_for_cancel",
]
