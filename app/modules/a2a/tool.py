"""``send_message`` 统一工具的执行：A2A 调用、a2a_tasks 落库、SSE 事件与 HITL 等待。

- 每次调用创建 ``a2a_tasks`` 行（submitted → working → 终态）；
- 远程支持 streaming 时走 ``message/stream``（SSE），否则 ``message/send``；
- ``input_required`` / ``auth_required`` 挂起等待 tasks 动作接口唤醒（进程内 waiter），
  唤醒后以同一 ``a2a_task_id`` / ``context_id`` 继续远程调用，SSE 流保持；
- 远程返回内容按不可信数据处理：截断、作为数据返回，不执行其中指令。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import get_settings
from app.db.models.remote import A2aTask, RemoteAgent
from app.db.session import get_session_factory
from app.modules.a2a import A2aClient, A2aError
from app.modules.a2a.taskstate import append_timeline, task_duration_ms, wake_decision_for
from app.modules.agents.service import _resolved_headers
from app.modules.chat.events import RunEventPublisher
from app.modules.chat.registry import registry

logger = logging.getLogger(__name__)

_MAX_TOOL_RESULT_CHARS = 4000
_TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}


@dataclass
class SendMessageOutcome:
    content: str
    summary: str
    status: str = "ok"  # ok / rejected


def _now() -> datetime:
    return datetime.now(UTC)


def _summary(text: str, limit: int = 500) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


def _status_text(status_message: dict[str, Any] | None) -> str:
    if not status_message:
        return ""
    parts = status_message.get("parts") or []
    chunks = [
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(chunks)


def _metadata_of(update: Any, artifact: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(artifact, dict) and isinstance(artifact.get("metadata"), dict):
        return dict(artifact["metadata"])
    status = getattr(update, "status_message", None)
    if isinstance(status, dict) and isinstance(status.get("metadata"), dict):
        return dict(status["metadata"])
    return {}


def _build_artifact(
    *, kind_hint: str, parts_text: list[str], metadata: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": str(metadata.get("type") or kind_hint or "text")[:32],
        "summary": _summary("\n".join(parts_text)),
        "parts": [item[:_MAX_TOOL_RESULT_CHARS] for item in parts_text],
        "data": {str(key): value for key, value in metadata.items() if key != "type"},
    }


def _merge_artifact(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if current is None:
        return incoming
    parts = list(current.get("parts") or []) + list(incoming.get("parts") or [])
    data = dict(current.get("data") or {})
    data.update(incoming.get("data") or {})
    return {
        "type": str(current.get("type") or incoming.get("type") or "text")[:32],
        "summary": _summary("\n".join(parts)),
        "parts": parts,
        "data": data,
    }


def _update_artifacts(task: A2aTask, update: Any) -> dict[str, Any] | None:
    """把 update 中的 artifact 并入任务结果；返回并入后的 artifact。"""
    artifacts: list[dict[str, Any]] = []
    if getattr(update, "artifact", None) is not None:
        artifacts.append(dict(update.artifact))
    artifacts.extend(dict(item) for item in (getattr(update, "artifacts", None) or []) if isinstance(item, dict))
    merged: dict[str, Any] | None = None
    for artifact in artifacts:
        parts_text: list[str] = []
        for part in artifact.get("parts") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts_text.append(str(part["text"]))
        metadata = dict(artifact.get("metadata") or {}) if isinstance(artifact, dict) else {}
        if not parts_text and not metadata:
            continue
        incoming = _build_artifact(kind_hint="text", parts_text=parts_text, metadata=metadata)
        merged = _merge_artifact(merged, incoming)
    if merged is not None:
        result = dict(task.result or {})
        result["artifact"] = _merge_artifact(result.get("artifact"), merged)
        result["summary"] = result["artifact"]["summary"]
        task.result = result
    return merged


async def _persist_task(task: A2aTask) -> None:
    async with get_session_factory()() as db:
        row = await db.get(A2aTask, task.id)
        if row is None:
            return
        for column in (
            "a2a_task_id",
            "context_id",
            "state",
            "progress",
            "request",
            "result",
            "error",
            "input_request",
            "auth_request",
            "timeline",
            "started_at",
            "finished_at",
        ):
            setattr(row, column, getattr(task, column))
        await db.commit()


class SendMessageRunner:
    """一次 send_message 工具调用的执行上下文。"""

    def __init__(
        self,
        *,
        run_id: uuid.UUID,
        publisher: RunEventPublisher,
        agent: RemoteAgent,
        task_text: str,
        context_text: str | None,
        conversation_id: uuid.UUID | None,
        tool_call_id: str,
    ) -> None:
        self.run_id = run_id
        self.publisher = publisher
        self.agent = agent
        self.task_text = task_text
        self.context_text = context_text
        self.conversation_id = conversation_id
        self.tool_call_id = tool_call_id
        self.task: A2aTask | None = None

    # ------------------------------------------------------------------ 事件

    def _agent_detail(self) -> dict[str, str]:
        return {
            "id": str(self.agent.id),
            "slug": self.agent.slug,
            "name": self.agent.name,
        }

    async def _emit_task_event(self, *, result_summary: str | None = None, error: dict | None = None) -> None:
        task = self.task
        if task is None:
            return
        await self.publisher.emit(
            "a2a.task",
            {
                "task_id": str(task.id),
                "a2a_task_id": task.a2a_task_id,
                "agent": self.agent.slug,
                "agent_detail": self._agent_detail(),
                "state": task.state,
                "progress": task.progress,
                "request_summary": _summary(str((task.request or {}).get("summary") or ""), 200),
                "result_summary": result_summary,
                "duration_ms": task_duration_ms(task),
                "error": error,
            },
            event_type="a2a",
            title=f"远程任务：{self.agent.name}",
            subtitle=task.state,
        )

    async def _emit_artifact_event(self, artifact: dict[str, Any]) -> None:
        task = self.task
        if task is None:
            return
        await self.publisher.emit(
            "a2a.artifact",
            {
                "task_id": str(task.id),
                "a2a_task_id": task.a2a_task_id,
                "artifact": artifact,
            },
            event_type="a2a",
            title=f"远程产出：{self.agent.name}",
            subtitle=artifact.get("summary") or "",
        )

    # ------------------------------------------------------------------ 落库与状态

    async def _create_task_row(self) -> A2aTask:
        now = _now()
        task = A2aTask(
            a2a_task_id="",
            remote_agent_id=self.agent.id,
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            tool_call_id=self.tool_call_id,
            context_id=str(self.conversation_id) if self.conversation_id else None,
            state="submitted",
            progress=None,
            request={
                "tool": "send_message",
                "args": {
                    "agent": self.agent.slug,
                    "task": self.task_text[:_MAX_TOOL_RESULT_CHARS],
                    **({"context": self.context_text[:_MAX_TOOL_RESULT_CHARS]} if self.context_text else {}),
                },
                "summary": _summary(self.task_text, 500),
            },
            timeline=[],
            started_at=now,
        )
        append_timeline(task, "submitted")
        async with get_session_factory()() as db:
            db.add(task)
            await db.commit()
            await db.refresh(task)
        self.task = task
        return task

    async def _transition(self, state: str, *, note: str = "") -> None:
        task = self.task
        if task is None:
            return
        append_timeline(task, state, note=note)
        await _persist_task(task)

    # ------------------------------------------------------------------ 远程调用

    def _client(self) -> A2aClient:
        return A2aClient(
            self.agent.url,
            headers=_resolved_headers(self.agent),
            card=self.agent.card,
        )

    async def _send_round(
        self, text: str, *, context_id: str | None, task_id: str | None
    ) -> list[Any]:
        client = self._client()
        if (self.agent.capabilities or {}).get("streaming"):
            return [update async for update in client.stream_message(
                text=text, context_id=context_id, task_id=task_id
            )]
        return await client.send_message(text=text, context_id=context_id, task_id=task_id)

    async def _poll_until_terminal(self, updates: list[Any]) -> list[Any]:
        """非流式 message/send 返回非终态 Task 时轮询 tasks/get 直到终态/等待态。"""
        if not updates:
            return updates
        last = updates[-1]
        if (
            last.kind != "task"
            or last.state is None
            or last.state in _TERMINAL_STATES
            or last.state in {"input_required", "auth_required"}
        ):
            return updates
        task_id = last.task_id or (self.task.a2a_task_id if self.task else None)
        if not task_id:
            return updates
        settings = get_settings()
        deadline = _now() + timedelta(seconds=settings.a2a_client_timeout_seconds)
        client = self._client()
        while _now() < deadline:
            await asyncio.sleep(0.2)
            update = await client.get_task(task_id)
            if update is None:
                continue
            updates.append(update)
            if update.state in _TERMINAL_STATES | {"input_required", "auth_required"}:
                break
        else:
            raise A2aError(
                "a2a_timeout",
                f"远程 Agent 在 {settings.a2a_client_timeout_seconds:g}s 内未完成任务",
                retryable=True,
            )
        return updates

    # ------------------------------------------------------------------ HITL 等待

    async def _wait_decision(self) -> dict:
        """等待 tasks 动作接口的唤醒；返回 decision dict。"""
        settings = get_settings()
        task = self.task
        if task is None:
            return {"action": "expired"}
        context = registry.get(self.run_id)
        if context is None:
            # 运行上下文不存在（测试或已结束）：直接过期收尾。
            await self._expire_waiting()
            return {"action": "expired"}
        decision = context.take_a2a_decision(task.id)
        if decision is not None:
            return decision
        waiter = context.wait_for_a2a_task(task.id)
        timeout = settings.a2a_input_timeout_seconds
        try:
            await asyncio.wait_for(waiter.wait(), timeout=timeout)
        except TimeoutError:
            await self._expire_waiting()
            return {"action": "expired"}
        decision = context.take_a2a_decision(task.id)
        if decision is None:
            return {"action": "cancel"}
        return decision

    async def _expire_waiting(self) -> None:
        task = self.task
        if task is None or task.state not in {"input_required", "auth_required"}:
            return
        from app.modules.a2a.taskstate import is_expired

        if not is_expired(task):
            # 等满超时但 expires_at 未到（时钟偏差保护）：手动置过期。
            from app.modules.a2a.taskstate import expire_waiting

            payload = task.input_request if task.state == "input_required" else task.auth_request
            if isinstance(payload, dict):
                payload["expires_at"] = _now().isoformat()
            expire_waiting(task)
        else:
            from app.modules.a2a.taskstate import expire_waiting

            expire_waiting(task)
        await _persist_task(task)
        await self._emit_task_event()

    async def _wait_pending(self, kind: str) -> dict:
        """input_required / auth_required：等待决定（事件已在状态应用时发出）。"""
        del kind
        return await self._wait_decision()

    # ------------------------------------------------------------------ 主流程

    async def run(self) -> SendMessageOutcome:
        task = await self._create_task_row()
        await self._emit_task_event()

        text = self.task_text
        if self.context_text:
            text = f"{self.task_text}\n\n补充上下文：{self.context_text}"
        remote_task_id: str | None = None
        context_id = task.context_id
        terminal: dict[str, str] | None = None

        try:
            await self._transition("working")
            await self._emit_task_event()
            while terminal is None:
                updates = await self._send_round(text, context_id=context_id, task_id=remote_task_id)
                updates = await self._poll_until_terminal(updates)
                for update in updates:
                    if getattr(update, "task_id", None):
                        remote_task_id = update.task_id
                        if task.a2a_task_id != remote_task_id:
                            task.a2a_task_id = remote_task_id
                    if getattr(update, "context_id", None):
                        context_id = update.context_id
                        task.context_id = context_id
                    artifact = _update_artifacts(task, update)
                    if artifact is not None:
                        await self._emit_artifact_event(artifact)
                    if update.kind == "task" and update.state is not None:
                        await self._apply_state(update)
                        if update.state in _TERMINAL_STATES:
                            terminal = {"state": update.state}
                            break
                    elif update.kind == "message":
                        # 非任务模式的 Message 即最终结果。
                        task.result = {
                            "artifact": _build_artifact(
                                kind_hint="text",
                                parts_text=[_status_text(update.status_message)],
                                metadata={},
                            ),
                            "summary": _summary(_status_text(update.status_message)),
                        }
                        await self._transition("completed")
                        terminal = {"state": "completed"}
                        break
                if terminal is not None:
                    break
                if task.state == "input_required":
                    decision = await self._wait_pending("input_required")
                    action = str(decision.get("action") or "")
                    if action == "resume":
                        text = str(decision.get("input") or "").strip() or text
                        task.input_request = None
                        await self._transition("working", note="已补充输入，继续远程调用")
                        await self._emit_task_event()
                        continue
                    if action == "reject":
                        await self._emit_task_event()
                        return SendMessageOutcome(
                            content="用户拒绝了本次远程任务的补充请求，任务已终止。",
                            summary="rejected: 用户拒绝",
                            status="rejected",
                        )
                    await self._transition("canceled", note="等待超时" if action == "expired" else "已取消")
                    await self._emit_task_event()
                    return SendMessageOutcome(
                        content="远程任务已取消，未获得结果。",
                        summary="canceled",
                    )
                if task.state == "auth_required":
                    decision = await self._wait_pending("auth_required")
                    action = str(decision.get("action") or "")
                    if action == "authorize":
                        task.auth_request = None
                        await self._transition("working", note="已授权，继续远程调用")
                        await self._emit_task_event()
                        continue
                    if action == "reject":
                        await self._emit_task_event()
                        return SendMessageOutcome(
                            content="用户拒绝了本次远程任务的授权请求，任务已终止。",
                            summary="rejected: 用户拒绝授权",
                            status="rejected",
                        )
                    await self._transition("canceled", note="等待超时" if action == "expired" else "已取消")
                    await self._emit_task_event()
                    return SendMessageOutcome(
                        content="远程任务已取消，未获得结果。",
                        summary="canceled",
                    )
                break
        except A2aError as exc:
            await self._fail_task(exc)
            return SendMessageOutcome(
                content=f"远程 Agent「{self.agent.name}」调用失败：{exc.message}",
                summary=f"error: {exc.message[:200]}",
                status="ok",
            )
        except asyncio.CancelledError:
            await self._transition("canceled", note="运行取消")
            await self._emit_task_event()
            raise

        state = (terminal or {}).get("state") or task.state
        if state == "completed":
            artifact = (
                (task.result or {}).get("artifact") if task.result else None
            ) or {"type": "text", "summary": "", "parts": [], "data": {}}
            result_summary = artifact.get("summary") or ""
            await self._emit_task_event(result_summary=result_summary)
            text_out = _summary(
                artifact.get("summary") or "\n".join(artifact.get("parts") or []) or "（远程任务完成）",
                _MAX_TOOL_RESULT_CHARS,
            )
            return SendMessageOutcome(
                content=f"远程 Agent「{self.agent.name}」返回：{text_out}",
                summary=result_summary,
            )
        if state == "failed":
            error = task.error or {"code": "a2a_error", "message": "远程任务失败"}
            await self._emit_task_event(error=error)
            return SendMessageOutcome(
                content=f"远程 Agent「{self.agent.name}」任务失败：{error.get('message')}",
                summary=f"error: {str(error.get('message'))[:200]}",
            )
        if state == "rejected":
            await self._emit_task_event()
            return SendMessageOutcome(
                content="远程任务被拒绝，未获得结果。",
                summary="rejected",
                status="rejected",
            )
        # 远程 canceled：终态已落库，补事件。
        await self._emit_task_event()
        return SendMessageOutcome(
            content="远程任务已取消，未获得结果。",
            summary="canceled",
        )

    async def _apply_state(self, update: Any) -> None:
        task = self.task
        if task is None:
            return
        state = str(update.state)
        task.progress = update.progress
        if state == "input_required":
            metadata = _metadata_of(update, None)
            expires = metadata.get("expires_at") or (
                _now() + timedelta(seconds=get_settings().a2a_input_timeout_seconds)
            ).isoformat()
            task.input_request = {
                "question": _status_text(update.status_message) or str(metadata.get("question") or "")[:2000],
                "input_schema": metadata.get("input_schema")
                if isinstance(metadata.get("input_schema"), dict)
                else {"type": "string"},
                "expires_at": str(expires),
            }
            append_timeline(task, "input_required")
            await _persist_task(task)
            await self._emit_task_event()
            await self.publisher.emit(
                "a2a.input_required",
                {
                    "task_id": str(task.id),
                    "a2a_task_id": task.a2a_task_id,
                    "agent": self.agent.slug,
                    "agent_detail": self._agent_detail(),
                    "question": task.input_request["question"],
                    "input_schema": task.input_request["input_schema"],
                    "expires_at": task.input_request["expires_at"],
                },
                event_type="a2a",
                title=f"远程请求补充：{self.agent.name}",
                subtitle=task.input_request["question"][:80],
            )
            return
        if state == "auth_required":
            metadata = _metadata_of(update, None)
            scopes = metadata.get("scopes")
            expires = metadata.get("expires_at") or (
                _now() + timedelta(seconds=get_settings().a2a_input_timeout_seconds)
            ).isoformat()
            task.auth_request = {
                "scopes": [str(item) for item in scopes] if isinstance(scopes, list) else [],
                "authorization_url": str(metadata.get("authorization_url") or "")[:2000] or None,
                "expires_at": str(expires),
            }
            append_timeline(task, "auth_required")
            await _persist_task(task)
            await self._emit_task_event()
            await self.publisher.emit(
                "a2a.auth_required",
                {
                    "task_id": str(task.id),
                    "a2a_task_id": task.a2a_task_id,
                    "agent": self.agent.slug,
                    "agent_detail": self._agent_detail(),
                    "scopes": task.auth_request["scopes"],
                    "authorization_url": task.auth_request["authorization_url"],
                    "expires_at": task.auth_request["expires_at"],
                },
                event_type="a2a",
                title=f"远程请求授权：{self.agent.name}",
                subtitle=",".join(task.auth_request["scopes"]) or "需要授权",
            )
            return
        if state == "failed":
            message = _status_text(update.status_message) or "远程任务失败"
            task.error = {
                "code": "a2a_remote_error",
                "message": message[:500],
                "retryable": False,
                "at": _now().isoformat(),
            }
        append_timeline(task, state)
        await _persist_task(task)
        await self._emit_task_event()

    async def _fail_task(self, exc: A2aError) -> None:
        task = self.task
        if task is None:
            return
        task.error = {
            "code": exc.code,
            "message": exc.message[:500],
            "retryable": exc.retryable,
            "at": _now().isoformat(),
        }
        append_timeline(task, "failed", note=exc.code)
        await _persist_task(task)
        await self._emit_task_event(error=task.error)
        if task.run_id is not None:
            wake_decision_for("failed", task)


async def execute_send_message(
    *,
    run_id: uuid.UUID,
    publisher: RunEventPublisher,
    agent: RemoteAgent,
    task_text: str,
    context_text: str | None,
    conversation_id: uuid.UUID | None,
    tool_call_id: str,
) -> SendMessageOutcome:
    """send_message 工具入口：由 ToolRunner 在工具循环内调用。"""
    runner = SendMessageRunner(
        run_id=run_id,
        publisher=publisher,
        agent=agent,
        task_text=task_text,
        context_text=context_text,
        conversation_id=conversation_id,
        tool_call_id=tool_call_id,
    )
    return await runner.run()
