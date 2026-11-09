"""本地假 A2A Agent：可启动的 ASGI 应用，用于出站链路的测试与联调。

- 提供 ``/.well-known/agent-card.json``（兼容 agent.json）与 JSON-RPC 端点；
- 按 task 文本触发场景：success / slow / fail / input_required / auth_required / streaming；
- ``input_required`` 首次返回等待补充，同一 contextId 第二次调用返回完成（模拟 resume）；
- 记录全部调用历史，测试可断言远程收到的参数。

用法：

- 测试：``register_test_transport(url, httpx.MockTransport(fake.app))``（见 client.py）；
- 联调：``uv run python scripts/mock_a2a_agent.py`` 监听 127.0.0.1:9100。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


class FakeA2aAgent:
    """内存态假 Agent；``asgi`` 属性可直接交给 uvicorn 或 httpx.MockTransport。"""

    def __init__(
        self,
        *,
        name: str = "Fake Agent",
        slug: str = "fake",
        description: str = "测试用假远程智能体",
        url: str = "http://127.0.0.1:9100",
        streaming: bool = False,
        slow_seconds: float = 0.0,
        requires_approval: bool = False,
        base_path: str = "",
    ) -> None:
        self.name = name
        self.slug = slug
        self.description = description
        self.url = url.rstrip("/")
        self.streaming = streaming
        self.slow_seconds = slow_seconds
        self.requires_approval = requires_approval
        self.base_path = base_path.rstrip("/")
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.app = self._build_app()

    # ------------------------------------------------------------------ 状态

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self) -> dict[str, Any] | None:
        return self.calls[-1] if self.calls else None

    def text_of_last_call(self) -> str:
        call = self.last_call or {}
        parts = (call.get("message") or {}).get("parts") or []
        return "\n".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))

    def resume_inputs(self) -> list[str]:
        return [call["text"] for call in self.calls if call.get("resume")]

    # ------------------------------------------------------------------ Card

    def card(self) -> dict[str, Any]:
        skill = {
            "id": f"{self.slug}_task",
            "name": "通用任务",
            "description": "执行任意文本任务并返回结果",
            "tags": ["fake"],
            "examples": ["帮我写一段周报"],
        }
        if self.requires_approval:
            skill["metadata"] = {"requiresApproval": True}
        return {
            "protocolVersion": "1.0",
            "name": self.name,
            "description": self.description,
            "url": self.url,
            "version": "0.1.0",
            "capabilities": {"streaming": self.streaming, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [skill],
        }

    # ------------------------------------------------------------------ 场景

    def _scenario(self, text: str) -> str:
        lowered = text.lower()
        if "slow" in lowered:
            return "slow"
        if "fail" in lowered:
            return "fail"
        if "input_required" in lowered:
            return "input_required"
        if "auth_required" in lowered:
            return "auth_required"
        if "streaming" in lowered:
            return "streaming"
        return "success"

    def _expires_at(self) -> str:
        return (datetime.now(UTC) + timedelta(minutes=30)).isoformat()

    # ------------------------------------------------------------------ JSON-RPC

    async def _handle_rpc(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = str(payload.get("method") or "")
        params = payload.get("params") or {}
        request_id = payload.get("id") or uuid.uuid4().hex
        self.calls.append(
            {
                "method": method,
                "message": params.get("message") or {},
                "contextId": (params.get("message") or {}).get("contextId"),
                "taskId": (params.get("message") or {}).get("taskId"),
                "text": self._message_text((params.get("message") or {}).get("parts") or []),
            }
        )
        try:
            if method == "tasks/get":
                return {"jsonrpc": "2.0", "id": request_id, "result": self._task_by_id(params)}
            if method == "tasks/cancel":
                task = self._task_by_id(params)
                task["status"] = self._status("canceled", "已取消")
                return {"jsonrpc": "2.0", "id": request_id, "result": task}
            if method == "message/send":
                result = self._run_message(params, streaming=False)
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"unknown method {method}"},
                }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }

    def _task_by_id(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("id") or "")
        if task_id not in self.tasks:
            raise RuntimeError(f"task not found: {task_id}")
        return self.tasks[task_id]

    @staticmethod
    def _message_text(parts: Any) -> str:
        return "\n".join(
            str(part.get("text") or "")
            for part in (parts or [])
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )

    def _status(self, state: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "agent",
            "parts": [{"kind": "text", "text": text}],
        }
        if metadata:
            message["metadata"] = metadata
        return {"state": state, "message": message}

    def _run_message(self, params: dict[str, Any], *, streaming: bool) -> dict[str, Any]:
        message = params.get("message") or {}
        text = self._message_text(message.get("parts") or [])
        context_id = str(message.get("contextId") or "")
        resume_task_id = str(message.get("taskId") or "")
        scenario = self._scenario(text)

        # resume：同 context 的第二次调用（或带 taskId 的继续）。
        existing = None
        if resume_task_id and resume_task_id in self.tasks:
            existing = self.tasks[resume_task_id]
        elif context_id:
            existing = next(
                (task for task in self.tasks.values() if task.get("contextId") == context_id),
                None,
            )
        is_resume = existing is not None and existing.get("status", {}).get("state") in {
            "input-required",
            "auth-required",
        }

        if scenario in {"input_required", "auth_required"} and not is_resume:
            task_id = resume_task_id or f"task_{uuid.uuid4().hex[:12]}"
            if scenario == "input_required":
                state = "input-required"
                status = self._status(
                    state,
                    "请补充统计口径：按创建时间还是完成时间？",
                    {"input_schema": {"type": "string"}, "expires_at": self._expires_at()},
                )
            else:
                state = "auth-required"
                status = self._status(
                    state,
                    "需要你授权访问代码仓库",
                    {
                        "scopes": ["repo:read"],
                        "authorization_url": "https://example.com/authorize",
                        "expires_at": self._expires_at(),
                    },
                )
            task = {
                "id": task_id,
                "contextId": context_id or uuid.uuid4().hex,
                "kind": "task",
                "status": status,
                "artifacts": [],
            }
            self.tasks[task_id] = task
            return task

        if scenario == "fail":
            task_id = resume_task_id or f"task_{uuid.uuid4().hex[:12]}"
            task = {
                "id": task_id,
                "contextId": context_id or uuid.uuid4().hex,
                "kind": "task",
                "status": self._status("failed", "远程任务处理失败（模拟）"),
                "artifacts": [],
            }
            self.tasks[task_id] = task
            return task

        if scenario == "slow":
            if self.slow_seconds > 0:
                asyncio.get_event_loop().call_later(self.slow_seconds, lambda: None)
                # 同步 sleep 不可行（ASGI 异步），用 busy-wait 的异步 sleep 在调用方处理；
                # 这里标记慢任务，超时测试直接依赖 client 超时。
            task_id = resume_task_id or f"task_{uuid.uuid4().hex[:12]}"
            task = {
                "id": task_id,
                "contextId": context_id or uuid.uuid4().hex,
                "kind": "task",
                "status": self._status("working", "处理中…"),
                "artifacts": [],
            }
            self.tasks[task_id] = task
            return task

        # success / streaming / resume 完成。
        task_id = resume_task_id or existing["id"] if existing else f"task_{uuid.uuid4().hex[:12]}"
        if existing:
            task_id = existing["id"]
        prefix = "（已按补充输入执行）" if is_resume else ""
        result_text = f"{prefix}完成：{text[:120]} 的处理结果如下\n- 核对通过 12/12 项\n- 结论：数字一致"
        task = {
            "id": task_id,
            "contextId": context_id or uuid.uuid4().hex,
            "kind": "task",
            "status": self._status("completed", "完成"),
            "artifacts": [
                {
                    "name": "result",
                    "parts": [{"kind": "text", "text": result_text}],
                    "metadata": {"type": "text", "checks_passed": 12, "checks_total": 12},
                }
            ],
        }
        self.tasks[task_id] = task
        self.calls[-1]["resume"] = is_resume
        return task

    # ------------------------------------------------------------------ ASGI

    def _build_app(self) -> FastAPI:
        app = FastAPI(title=self.name)

        @app.get(f"{self.base_path}/.well-known/agent-card.json")
        @app.get(f"{self.base_path}/.well-known/agent.json")
        async def agent_card():
            return JSONResponse(self.card())

        @app.post(self.base_path or "/")
        async def jsonrpc(request: Request):
            payload = await request.json()
            if isinstance(payload, list):
                responses = [await self._handle_rpc(item) for item in payload]
                return JSONResponse(responses)
            method = str(payload.get("method") or "")
            if method == "message/stream":
                return StreamingResponse(
                    self._stream_events(payload), media_type="text/event-stream"
                )
            result = await self._handle_rpc(payload)
            return JSONResponse(result)

        return app

    async def _stream_events(self, payload: dict[str, Any]):
        request_id = payload.get("id") or uuid.uuid4().hex
        params = payload.get("params") or {}
        message = params.get("message") or {}
        text = self._message_text(message.get("parts") or [])
        context_id = str(message.get("contextId") or "")
        self.calls.append(
            {
                "method": "message/stream",
                "message": message,
                "contextId": context_id,
                "taskId": message.get("taskId"),
                "text": text,
            }
        )
        scenario = self._scenario(text)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        if scenario == "fail":
            task_id = f"task_{uuid.uuid4().hex[:12]}"
        elif scenario == "slow" and self.slow_seconds > 0:
            await asyncio.sleep(min(self.slow_seconds, 120))

        def frame(update: dict[str, Any]) -> str:
            return (
                "event: message\n"
                f"data: {json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': update}, ensure_ascii=False)}\n\n"
            )

        working = self._status("working", "流式处理中…")
        yield frame(
            {"kind": "status-update", "taskId": task_id, "contextId": context_id, "status": working, "final": False}
        )
        if scenario == "fail":
            failed = self._status("failed", "远程任务处理失败（模拟）")
            yield frame(
                {"kind": "status-update", "taskId": task_id, "contextId": context_id, "status": failed, "final": True}
            )
            return
        artifact = {
            "name": "result",
            "parts": [{"kind": "text", "text": f"流式结果：{text[:120]} 处理完成"}],
            "metadata": {"type": "text"},
        }
        yield frame(
            {"kind": "artifact-update", "taskId": task_id, "contextId": context_id, "artifact": artifact}
        )
        completed = self._status("completed", "完成")
        yield frame(
            {"kind": "status-update", "taskId": task_id, "contextId": context_id, "status": completed, "final": True}
        )
