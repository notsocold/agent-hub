"""A2A JSON-RPC 2.0 客户端（自研，兼容 0.3 / 1.0 方法名）。

a2a-sdk 1.x 的官方客户端以 protobuf 类型与多传输抽象为主，接入成本高于收益；
本项目沿用 M1.5 MCP 先例自研 httpx 客户端，仅使用 ``message/send``、
``message/stream``、``tasks/get``、``tasks/cancel`` 四个 0.3/1.0 通用方法。

设计要点：

- 出站不跟随重定向；连接超时/读超时受 ``A2A_CLIENT_TIMEOUT_SECONDS`` 控制；
- 响应体上限 ``A2A_MAX_RESPONSE_BYTES``，超出截断并标记；
- 错误统一映射为 ``A2aError``：``a2a_timeout`` / ``a2a_unavailable`` /
  ``a2a_auth_failed`` / ``a2a_remote_error`` / ``a2a_invalid_response``；
- 测试注入：``register_test_transport`` 按基础 URL 前缀替换底层 transport。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_STATE_MAP = {
    "input-required": "input_required",
    "auth-required": "auth_required",
}

# 测试注入：基础 URL → transport；生产路径为空。
_test_transports: dict[str, httpx.AsyncBaseTransport] = {}


def register_test_transport(url: str, transport: httpx.AsyncBaseTransport) -> None:
    """测试注入：对该基础 URL 的出站请求改走指定 transport。"""
    _test_transports[url.rstrip("/")] = transport


def clear_test_transports() -> None:
    _test_transports.clear()


def _test_transport_for(base_url: str) -> httpx.AsyncBaseTransport | None:
    return _test_transports.get(base_url.rstrip("/"))


class A2aError(Exception):
    """远程调用失败；code 为契约错误码，retryable 标记是否值得重试。"""

    def __init__(
        self, code: str, message: str, *, retryable: bool = True, rpc_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.rpc_code = rpc_code


@dataclass
class A2aUpdate:
    """一次远程响应的归一化更新。"""

    kind: str  # task / message / artifact / error
    task_id: str | None = None
    context_id: str | None = None
    message_id: str | None = None
    state: str | None = None
    status_message: dict[str, Any] | None = None
    progress: float | None = None
    final: bool | None = None
    artifact: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    parts: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None


def _map_state(raw: Any) -> str:
    state = str(raw or "")
    if state in _STATE_MAP:
        return _STATE_MAP[state]
    if state in {
        "submitted",
        "working",
        "input_required",
        "auth_required",
        "completed",
        "failed",
        "canceled",
        "rejected",
    }:
        return state
    return "working"


def _parse_message(raw: Any) -> A2aUpdate:
    parts = [dict(item) for item in raw.get("parts", []) if isinstance(item, dict)]
    context_id = raw.get("contextId")
    return A2aUpdate(
        kind="message",
        message_id=str(raw.get("messageId") or ""),
        context_id=str(context_id) if context_id else None,
        parts=parts,
        status_message={"role": raw.get("role"), "parts": parts},
    )


def _parse_status(raw: Any) -> tuple[str, dict[str, Any] | None, float | None]:
    if not isinstance(raw, dict):
        return "working", None, None
    state = _map_state(raw.get("state"))
    message = raw.get("message")
    status_message = dict(message) if isinstance(message, dict) else None
    progress = raw.get("progress")
    if progress is None and isinstance(status_message, dict):
        metadata = status_message.get("metadata")
        if isinstance(metadata, dict) and metadata.get("progress") is not None:
            try:
                progress = float(metadata["progress"])
            except (TypeError, ValueError):
                progress = None
    return state, status_message, progress


def _parse_task(raw: Any) -> A2aUpdate:
    state, status_message, progress = _parse_status(raw.get("status"))
    task_id = str(raw.get("id") or "")
    context_id = raw.get("contextId")
    artifacts = [
        dict(item) for item in (raw.get("artifacts") or []) if isinstance(item, dict)
    ]
    return A2aUpdate(
        kind="task",
        task_id=task_id,
        context_id=str(context_id) if context_id else None,
        state=state,
        status_message=status_message,
        progress=progress,
        artifacts=artifacts,
    )


def _parse_result(result: Any) -> A2aUpdate | None:
    """解析 JSON-RPC result：Task / TaskStatusUpdateEvent / TaskArtifactUpdateEvent / Message。"""
    if not isinstance(result, dict):
        return None
    kind = result.get("kind")
    if kind == "status-update":
        state, status_message, progress = _parse_status(result.get("status"))
        task_id = str(result.get("taskId") or result.get("id") or "")
        context_id = result.get("contextId")
        return A2aUpdate(
            kind="task",
            task_id=task_id,
            context_id=str(context_id) if context_id else None,
            state=state,
            status_message=status_message,
            progress=progress,
            final=bool(result.get("final", False)),
        )
    if kind == "artifact-update":
        task_id = str(result.get("taskId") or "")
        context_id = result.get("contextId")
        artifact = result.get("artifact")
        return A2aUpdate(
            kind="artifact",
            task_id=task_id,
            context_id=str(context_id) if context_id else None,
            artifact=dict(artifact) if isinstance(artifact, dict) else None,
        )
    if kind == "message":
        return _parse_message(result)
    if "status" in result and ("id" in result or "taskId" in result):
        return _parse_task(result)
    return None


def _parse_envelope(data: Any) -> A2aUpdate | None:
    """解析一条 JSON 负载：JSON-RPC 信封（result/error）或裸 Task/Message。"""
    if not isinstance(data, dict):
        return None
    if "result" in data:
        return _parse_result(data.get("result"))
    if "error" in data:
        error = data.get("error")
        if isinstance(error, dict):
            return A2aUpdate(
                kind="error",
                error={
                    "code": str(error.get("code") or ""),
                    "message": str(error.get("message") or "远程返回 JSON-RPC 错误"),
                },
            )
        return A2aUpdate(kind="error", error={"code": "", "message": str(error)})
    # 非信封的裸负载（宽松兼容）。
    return _parse_result(data)


def _text_of_parts(parts: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks)


class A2aClient:
    """单个远程 Agent 的 JSON-RPC 客户端。"""

    def __init__(
        self,
        agent_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = agent_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout if timeout is not None else settings.a2a_client_timeout_seconds
        self.max_response_bytes = settings.a2a_max_response_bytes

    # ------------------------------------------------------------------ 请求

    async def _post_jsonrpc(
        self, method: str, params: dict[str, Any], *, stream: bool = False
    ) -> httpx.Response:
        transport = _test_transport_for(self.base_url)
        request_id = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        headers.update(self.headers)
        client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        try:
            return await client.post(self.base_url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise A2aError(
                "a2a_timeout",
                f"远程 Agent 在 {self.timeout:g}s 内未响应",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise A2aError(
                "a2a_unavailable",
                f"无法连接远程 Agent：{exc.__class__.__name__}",
                retryable=True,
            ) from exc
        finally:
            await client.aclose()

    def _raise_http_error(self, response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise A2aError(
                "a2a_auth_failed",
                "远程 Agent 鉴权失败，请检查凭据",
                retryable=False,
            )
        if 400 <= response.status_code < 500:
            raise A2aError(
                "a2a_unavailable",
                f"远程 Agent 拒绝请求（HTTP {response.status_code}）",
                retryable=False,
            )
        raise A2aError(
            "a2a_unavailable",
            f"远程 Agent 服务异常（HTTP {response.status_code}）",
            retryable=True,
        )

    # ------------------------------------------------------------------ 解析

    def _parse_non_stream(self, response: httpx.Response) -> list[A2aUpdate]:
        if response.status_code != 200:
            self._raise_http_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise A2aError("a2a_invalid_response", "远程返回内容不是合法 JSON", retryable=False) from exc
        update = _parse_envelope(data)
        if update is None:
            raise A2aError("a2a_invalid_response", "远程返回结构无法识别", retryable=False)
        if update.kind == "error":
            self._raise_rpc_error(update)
        return [update]

    def _raise_rpc_error(self, update: A2aUpdate) -> None:
        error = update.error or {}
        raise A2aError(
            "a2a_remote_error",
            str(error.get("message") or "远程返回 JSON-RPC 错误"),
            retryable=False,
            rpc_code=_safe_int(error.get("code")),
        )

    # ------------------------------------------------------------------ 公共方法

    async def send_message(
        self,
        *,
        text: str,
        context_id: str | None = None,
        task_id: str | None = None,
        message_id: str | None = None,
    ) -> list[A2aUpdate]:
        """非流式 ``message/send``；返回归一化更新列表。"""
        message: dict[str, Any] = {
            "role": "user",
            "messageId": message_id or uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        params: dict[str, Any] = {
            "message": message,
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        }
        response = await self._post_jsonrpc("message/send", params)
        return self._parse_non_stream(response)

    async def stream_message(
        self,
        *,
        text: str,
        context_id: str | None = None,
        task_id: str | None = None,
        message_id: str | None = None,
    ) -> AsyncIterator[A2aUpdate]:
        """流式 ``message/stream``：SSE 事件逐个 yield（含终态 task 更新）。"""
        message: dict[str, Any] = {
            "role": "user",
            "messageId": message_id or uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        params: dict[str, Any] = {
            "message": message,
            "configuration": {"acceptedOutputModes": ["text/plain"]},
        }
        response = await self._post_jsonrpc("message/stream", params, stream=True)
        if response.status_code != 200:
            self._raise_http_error(response)
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type:
            # 非 SSE 响应：按普通信封解析（宽松兼容）。
            try:
                update = _parse_envelope(response.json())
            except ValueError as exc:
                raise A2aError(
                    "a2a_invalid_response", "远程流式响应不是 SSE", retryable=False
                ) from exc
            if update is None:
                raise A2aError("a2a_invalid_response", "远程流式响应无法识别", retryable=False)
            if update.kind == "error":
                self._raise_rpc_error(update)
            yield update
            return

        current_event: str | None = None
        size = 0
        async for line in response.aiter_lines():
            size += len(line) + 1
            if size > self.max_response_bytes:
                yield A2aUpdate(
                    kind="error",
                    error={"code": "a2a_invalid_response", "message": "远程流式响应体过大"},
                )
                return
            if line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
                continue
            if not line.startswith("data:"):
                continue
            raw = line.split(":", 1)[1].strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            update = _parse_envelope(data)
            if update is None:
                continue
            if update.kind == "error":
                self._raise_rpc_error(update)
            update.message_id = update.message_id or current_event
            yield update

    async def get_task(self, task_id: str) -> A2aUpdate | None:
        """``tasks/get``：查询远程任务状态。"""
        response = await self._post_jsonrpc(
            "tasks/get", {"id": task_id, "historyLength": 10}
        )
        updates = self._parse_non_stream(response)
        return updates[0] if updates else None

    async def cancel_task(self, task_id: str) -> A2aUpdate | None:
        """``tasks/cancel``：尽力取消远程任务。"""
        try:
            response = await self._post_jsonrpc("tasks/cancel", {"id": task_id})
            updates = self._parse_non_stream(response)
            return updates[0] if updates else None
        except A2aError as exc:
            logger.info("远程取消失败（尽力而为）task=%s：%s", task_id, exc.message)
            return None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def artifact_text(artifact: dict[str, Any] | None) -> str:
    """从 Artifact 提取纯文本内容。"""
    if not artifact:
        return ""
    parts = artifact.get("parts") or []
    return _text_of_parts([dict(part) for part in parts if isinstance(part, dict)])
