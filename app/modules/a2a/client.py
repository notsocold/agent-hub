"""A2A 出站客户端：基于 ``a2a-sdk`` 1.3.0 的 JSON-RPC 传输（兼容 0.3 / 1.0）。

迁移说明（2026-11-10）：协议层改用官方 ``a2a-sdk``（Agent Card 解析、JSON-RPC
transport、protobuf 类型、SDK 错误类型），对外仍保留 ``A2aClient`` / ``A2aError``
/ ``A2aUpdate`` 语义，业务层（``tool.py``、``tasks``、``agents``）无需感知 SDK 类型。

- 版本协商：Agent Card 的 JSONRPC interface 为 0.3.x 时用 SDK 的
  ``CompatJsonRpcTransport``，1.x 时用 ``JsonRpcTransport``；
- card 优先由调用方传入（数据库快照），缺失时通过 SDK ``A2ACardResolver`` 拉取；
- 测试注入：``register_test_transport`` 保留，SDK 传输共用同一 ``httpx.AsyncClient``；
- 响应体上限仍按 ``A2A_MAX_RESPONSE_BYTES`` 约束（按 UTF-8 字节估算）。
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from a2a.client.card_resolver import A2ACardResolver, parse_agent_card
from a2a.client.errors import A2AClientTimeoutError
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.compat.v0_3.jsonrpc_transport import CompatJsonRpcTransport
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    SendMessageResponse,
    StreamResponse,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import A2AError as SdkA2AError
from google.protobuf import json_format

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 测试注入：基础 URL → transport；生产路径为空。
_test_transports: dict[str, httpx.AsyncBaseTransport] = {}

_TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}

_STATE_NAMES: dict[int, str] = {
    TaskState.TASK_STATE_SUBMITTED: "submitted",
    TaskState.TASK_STATE_WORKING: "working",
    TaskState.TASK_STATE_COMPLETED: "completed",
    TaskState.TASK_STATE_FAILED: "failed",
    TaskState.TASK_STATE_CANCELED: "canceled",
    TaskState.TASK_STATE_INPUT_REQUIRED: "input_required",
    TaskState.TASK_STATE_REJECTED: "rejected",
    TaskState.TASK_STATE_AUTH_REQUIRED: "auth_required",
}

_HTTP_STATUS_RE = re.compile(r"HTTP\s*(?:Error)?\s*:?\s*(\d{3})")


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


# --------------------------------------------------------------------------- protobuf → dict


def _struct_dict(struct: Any) -> dict[str, Any]:
    """protobuf Struct → 普通 dict；空 Struct 返回空 dict。"""
    if struct is None or not struct.ListFields():
        return {}
    return dict(json_format.MessageToDict(struct))


def _part_dict(part: Part) -> dict[str, Any]:
    if part.text:
        return {"kind": "text", "text": part.text}
    if part.url:
        return {"kind": "file", "file": {"uri": part.url}}
    if part.HasField("data"):
        return {"kind": "data", "data": _struct_value_dict(part.data)}
    return {"kind": "text", "text": ""}


def _struct_value_dict(value: Any) -> Any:
    """protobuf Value → Python 值。"""
    return json_format.MessageToDict(value)


def _message_dict(message: Message) -> dict[str, Any]:
    """把 protobuf Message 转成 0.3 风格的 dict，供 tool.py 的既有解析逻辑复用。"""
    return {
        "role": "agent" if message.role == Role.ROLE_AGENT else "user",
        "messageId": message.message_id,
        "parts": [_part_dict(part) for part in message.parts],
        "metadata": _struct_dict(message.metadata),
    }


def _artifact_dict(artifact: Any) -> dict[str, Any]:
    return {
        "artifactId": artifact.artifact_id,
        "name": artifact.name,
        "description": artifact.description,
        "parts": [_part_dict(part) for part in artifact.parts],
        "metadata": _struct_dict(artifact.metadata),
    }


def _state_str(state: Any) -> str:
    if isinstance(state, str):
        return _STATE_NAMES.get(_enum_value(state), "working")
    return _STATE_NAMES.get(int(state), "working")


def _enum_value(name: str) -> int:
    return int(TaskState.Value(name)) if name in TaskState.keys() else 0


def _progress_of(message: Message | None) -> float | None:
    if message is None:
        return None
    metadata = _struct_dict(message.metadata)
    raw = metadata.get("progress")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _task_update(task: Task) -> A2aUpdate:
    status = task.status
    message = status.message if status.HasField("message") else None
    state = _state_str(status.state)
    return A2aUpdate(
        kind="task",
        task_id=task.id,
        context_id=task.context_id,
        state=state,
        status_message=_message_dict(message) if message is not None else None,
        progress=_progress_of(message),
        final=state in _TERMINAL_STATES,
        artifacts=[_artifact_dict(artifact) for artifact in task.artifacts],
    )


def _message_update(message: Message) -> A2aUpdate:
    return A2aUpdate(
        kind="message",
        message_id=message.message_id,
        context_id=message.context_id,
        parts=[_part_dict(part) for part in message.parts],
        status_message=_message_dict(message),
    )


def _status_update(event: TaskStatusUpdateEvent) -> A2aUpdate:
    status = event.status
    message = status.message if status.HasField("message") else None
    state = _state_str(status.state)
    return A2aUpdate(
        kind="task",
        task_id=event.task_id,
        context_id=event.context_id,
        state=state,
        status_message=_message_dict(message) if message is not None else None,
        progress=_progress_of(message),
        final=state in _TERMINAL_STATES,
    )


def _stream_update(event: StreamResponse) -> A2aUpdate | None:
    if event.HasField("task"):
        return _task_update(event.task)
    if event.HasField("message"):
        return _message_update(event.message)
    if event.HasField("status_update"):
        return _status_update(event.status_update)
    if event.HasField("artifact_update"):
        return A2aUpdate(
            kind="artifact",
            task_id=event.artifact_update.task_id,
            context_id=event.artifact_update.context_id,
            artifact=_artifact_dict(event.artifact_update.artifact),
        )
    return None


def _response_updates(response: SendMessageResponse) -> list[A2aUpdate]:
    if response.HasField("task"):
        return [_task_update(response.task)]
    if response.HasField("message"):
        return [_message_update(response.message)]
    raise A2aError("a2a_invalid_response", "远程返回结构无法识别", retryable=False)


def _text_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _update_size(update: A2aUpdate) -> int:
    """按 UTF-8 字节数估算一次更新的体积。"""
    total = 0
    for part in update.parts:
        total += _text_bytes(str(part.get("text") or ""))
    if update.status_message is not None:
        for part in update.status_message.get("parts") or []:
            total += _text_bytes(str(part.get("text") or ""))
    if update.artifact is not None:
        for part in update.artifact.get("parts") or []:
            total += _text_bytes(str(part.get("text") or ""))
    for artifact in update.artifacts:
        for part in artifact.get("parts") or []:
            total += _text_bytes(str(part.get("text") or ""))
    return total


def _check_size(total: int) -> None:
    if total > get_settings().a2a_max_response_bytes:
        raise A2aError("a2a_invalid_response", "远程响应体过大", retryable=False)


# --------------------------------------------------------------------------- 错误映射


def _extract_http_status(message: str) -> int | None:
    match = _HTTP_STATUS_RE.search(message)
    return int(match.group(1)) if match else None


def _map_exception(exc: Exception, timeout: float) -> A2aError:
    if isinstance(exc, A2AClientTimeoutError | httpx.TimeoutException):
        return A2aError("a2a_timeout", f"远程 Agent 在 {timeout:g}s 内未响应", retryable=True)
    if isinstance(exc, httpx.RequestError):
        return A2aError(
            "a2a_unavailable",
            f"无法连接远程 Agent：{exc.__class__.__name__}",
            retryable=True,
        )
    if isinstance(exc, SdkA2AError):
        message = str(getattr(exc, "message", exc) or "")
        status = _extract_http_status(message)
        if status in {401, 403}:
            return A2aError("a2a_auth_failed", "远程 Agent 鉴权失败，请检查凭据", retryable=False)
        if status is not None and 400 <= status < 500:
            return A2aError(
                "a2a_unavailable", f"远程 Agent 拒绝请求（HTTP {status}）", retryable=False
            )
        if status is not None and status >= 500:
            return A2aError(
                "a2a_unavailable", f"远程 Agent 服务异常（HTTP {status}）", retryable=True
            )
        lowered = message.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return A2aError("a2a_timeout", "远程 Agent 响应超时", retryable=True)
        return A2aError(
            "a2a_remote_error", message or "远程 Agent 返回错误", retryable=False
        )
    return A2aError(
        "a2a_invalid_response",
        f"远程返回无法识别：{exc.__class__.__name__}",
        retryable=False,
    )


# --------------------------------------------------------------------------- 客户端


class A2aClient:
    """单个远程 Agent 的 SDK JSON-RPC 客户端（对外保持 M2 语义）。"""

    def __init__(
        self,
        agent_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        card: dict[str, Any] | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = agent_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout if timeout is not None else settings.a2a_client_timeout_seconds
        # Agent Card 快照（数据库原样 JSON）；为空时由 SDK resolver 拉取。
        self.card = card or None

    # ------------------------------------------------------------------ 传输

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[Any]:
        transport = _test_transport_for(self.base_url)
        http = httpx.AsyncClient(
            timeout=self.timeout,
            headers=self.headers,
            follow_redirects=False,
            transport=transport,
            trust_env=False,
        )
        try:
            card = await self._resolve_card(http)
            yield self._build_transport(http, card)
        finally:
            await http.aclose()

    async def _resolve_card(self, http: httpx.AsyncClient) -> Any:
        if self.card:
            # parse_agent_card 会原地 pop 0.3 字段，必须深拷贝，避免污染数据库快照。
            return parse_agent_card(copy.deepcopy(self.card))
        try:
            return await A2ACardResolver(http, self.base_url).get_agent_card()
        except Exception as exc:
            raise _map_exception(exc, self.timeout) from exc

    def _build_transport(self, http: httpx.AsyncClient, card: Any) -> Any:
        interfaces = [item for item in card.supported_interfaces if item.url]
        if not interfaces:
            raise A2aError(
                "a2a_invalid_response",
                "Agent Card 缺少可用的 JSON-RPC interface",
                retryable=False,
            )
        interface = next(
            (item for item in interfaces if item.protocol_binding == "JSONRPC"),
            interfaces[0],
        )
        url = interface.url or self.base_url
        if (interface.protocol_version or "1.0").startswith("0.3"):
            return CompatJsonRpcTransport(http, card, url)
        return JsonRpcTransport(http, card, url)

    def _build_request(
        self,
        *,
        text: str,
        context_id: str | None,
        task_id: str | None,
        message_id: str | None,
    ) -> SendMessageRequest:
        message = Message(
            role=Role.ROLE_USER,
            message_id=message_id or uuid.uuid4().hex,
            parts=[Part(text=text)],
        )
        if context_id:
            message.context_id = context_id
        if task_id:
            message.task_id = task_id
        return SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(accepted_output_modes=["text/plain"]),
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
        request = self._build_request(
            text=text, context_id=context_id, task_id=task_id, message_id=message_id
        )
        async with self._open() as transport:
            try:
                response = await transport.send_message(request)
            except Exception as exc:
                raise _map_exception(exc, self.timeout) from exc
        updates = _response_updates(response)
        _check_size(sum(_update_size(update) for update in updates))
        return updates

    async def stream_message(
        self,
        *,
        text: str,
        context_id: str | None = None,
        task_id: str | None = None,
        message_id: str | None = None,
    ) -> AsyncIterator[A2aUpdate]:
        """流式 ``message/stream``：SSE 事件逐个 yield（含终态 task 更新）。"""
        request = self._build_request(
            text=text, context_id=context_id, task_id=task_id, message_id=message_id
        )
        total = 0
        async with self._open() as transport:
            try:
                async for event in transport.send_message_streaming(request):
                    update = _stream_update(event)
                    if update is None:
                        continue
                    total += _update_size(update)
                    _check_size(total)
                    yield update
            except Exception as exc:
                raise _map_exception(exc, self.timeout) from exc

    async def get_task(self, task_id: str) -> A2aUpdate | None:
        """``tasks/get``：查询远程任务状态。"""
        async with self._open() as transport:
            try:
                task = await transport.get_task(GetTaskRequest(id=task_id))
            except Exception as exc:
                raise _map_exception(exc, self.timeout) from exc
        update = _task_update(task)
        _check_size(_update_size(update))
        return update

    async def cancel_task(self, task_id: str) -> A2aUpdate | None:
        """``tasks/cancel``：尽力取消远程任务。"""
        try:
            async with self._open() as transport:
                task = await transport.cancel_task(CancelTaskRequest(id=task_id))
            return _task_update(task)
        except Exception as exc:
            mapped = _map_exception(exc, self.timeout)
            logger.info("远程取消失败（尽力而为）task=%s：%s", task_id, mapped.message)
            return None


def artifact_text(artifact: dict[str, Any] | None) -> str:
    """从 Artifact 提取纯文本内容。"""
    if not artifact:
        return ""
    parts = artifact.get("parts") or []
    return "\n".join(
        str(part.get("text") or "") for part in parts if isinstance(part, dict)
    )


__all__ = [
    "A2aClient",
    "A2aError",
    "A2aUpdate",
    "artifact_text",
    "clear_test_transports",
    "register_test_transport",
]
