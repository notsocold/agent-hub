"""MCP Client：Streamable HTTP / SSE 的 JSON-RPC 2.0 客户端（httpx 实现）。

握手顺序：initialize → notifications/initialized → tools/list → tools/call；
兼容 ``application/json`` 与 ``text/event-stream`` 响应；SSE 传输通过
``GET`` 建立会话取得 ``endpoint`` 后向其 POST。stdlib 传输由 StdioMcpClient
用换行分隔 JSON-RPC 实现（仅系统/管理员配置，默认关闭）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.errors import ServiceUnavailableError, ValidationFailedError

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "agent-hub", "version": "0.1.0"}

_SSE_DATA_PATTERN = re.compile(r"^data:\s*(.*)$", re.MULTILINE)


def parse_sse_body(body: str) -> list[dict[str, Any]]:
    """解析 SSE 响应体：提取所有 ``data:`` 行的 JSON 对象。"""
    results: list[dict[str, Any]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            results.append(parsed)
    return results


def parse_json_or_sse(content_type: str, body: bytes) -> list[dict[str, Any]]:
    """按 Content-Type 解析响应体，统一返回 JSON-RPC 消息列表。"""
    text = body.decode("utf-8", errors="replace")
    if "text/event-stream" in (content_type or "").lower():
        return parse_sse_body(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ServiceUnavailableError("MCP 响应不是合法 JSON", code="mcp_protocol_error") from exc
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    raise ServiceUnavailableError("MCP 响应格式不合法", code="mcp_protocol_error")


def _cap_size(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    if len(raw.encode()) <= max_bytes:
        return payload
    return {"_truncated": True, "error": f"响应超过 {max_bytes} 字节，已截断", "size": len(raw.encode())}


@dataclass
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any]


class McpHttpClient:
    """远程 MCP 服务器客户端；``transport`` 注入用于测试（httpx.MockTransport）。"""

    def __init__(
        self,
        *,
        url: str,
        transport: str = "streamable_http",
        headers: dict[str, str] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self.url = url.rstrip("/")
        self.transport = transport
        self.headers = {**(headers or {})}
        self.timeout = timeout_seconds or settings.mcp_client_timeout_seconds
        self._http_transport = http_transport
        self._max_response = settings.mcp_max_response_bytes
        self._endpoint: str | None = None  # SSE 会话的 POST 端点
        self._initialized = False
        self.last_error: str | None = None
        self.latency_ms: int | None = None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._http_transport,
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=False,
        )

    # ------------------------------------------------------------------ 传输

    async def _open_sse_session(self, client: httpx.AsyncClient) -> str:
        """GET 建立 SSE 会话，返回服务端给出的 POST endpoint。"""
        response = await client.get(
            self.url,
            headers={**self.headers, "Accept": "text/event-stream"},
        )
        if response.status_code != 200:
            raise ServiceUnavailableError(
                f"MCP SSE 握手失败：HTTP {response.status_code}", code="mcp_unreachable"
            )
        for message in parse_sse_body(response.text):
            event = message.get("event") or ""
            data = message.get("data")
            if event == "endpoint" and isinstance(data, str):
                return data
            if isinstance(data, dict) and "uri" in data:  # 部分实现直接给 uri
                return str(data["uri"])
        raise ServiceUnavailableError("MCP SSE 握手未返回 endpoint", code="mcp_protocol_error")

    async def _post(
        self, client: httpx.AsyncClient, message: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if self.transport == "sse" and self._endpoint is None:
            self._endpoint = await self._open_sse_session(client)
        target = self._endpoint if self._endpoint else self.url
        headers = {
            **self.headers,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.transport == "streamable_http":
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        response = await client.post(target, json=message, headers=headers)
        if response.status_code == 405 and self.transport == "streamable_http" and self._endpoint is None:
            # 老式 SSE-only 服务器：POST 主地址 405，走 GET 会话流程。
            self._endpoint = await self._open_sse_session(client)
            response = await client.post(
                self._endpoint,
                json=message,
                headers={**self.headers, "Accept": "application/json, text/event-stream"},
            )
        if response.status_code == 202 or not response.content:
            return []
        if response.status_code >= 400:
            raise ServiceUnavailableError(
                f"MCP 请求失败：HTTP {response.status_code}",
                code="mcp_unreachable",
            )
        return parse_json_or_sse(response.headers.get("content-type", ""), response.content)

    async def _rpc(self, message: dict[str, Any]) -> dict[str, Any] | None:
        async with self._client() as client:
            messages = await self._post(client, message)
            request_id = message.get("id")
            if request_id is None:  # 通知无需响应
                return None
            for item in messages:
                if item.get("id") == request_id:
                    return item
            if messages and messages[-1].get("id") in (None, request_id):
                return messages[-1]
        raise ServiceUnavailableError("MCP 响应缺少对应 id", code="mcp_protocol_error")

    # ------------------------------------------------------------------ 业务

    async def initialize(self) -> None:
        result = await self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            }
        )
        if result is None or "result" not in result:
            raise ServiceUnavailableError("MCP initialize 失败", code="mcp_protocol_error")
        await self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._initialized = True

    async def list_tools(self) -> list[ToolInfo]:
        if not self._initialized:
            await self.initialize()
        result = await self._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        if result is None or "result" not in result:
            raise ServiceUnavailableError("MCP tools/list 失败", code="mcp_protocol_error")
        tools: list[ToolInfo] = []
        for item in result["result"].get("tools", []):
            if not isinstance(item, dict):
                continue
            tools.append(
                ToolInfo(
                    name=str(item.get("name") or "")[:128],
                    description=str(item.get("description") or "")[:4000],
                    input_schema=item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {},
                    annotations=item.get("annotations") if isinstance(item.get("annotations"), dict) else {},
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self._initialized:
            await self.initialize()
        result = await self._rpc(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if result is None:
            raise ServiceUnavailableError("MCP tools/call 无响应", code="mcp_protocol_error")
        if "error" in result:
            error = result["error"]
            raise ServiceUnavailableError(
                f"MCP 工具调用失败：{str(error.get('message') or '未知错误')[:300]}",
                code="mcp_tool_error",
            )
        if "result" not in result:
            raise ServiceUnavailableError("MCP tools/call 响应缺少 result", code="mcp_protocol_error")
        return _cap_size(result["result"], self._max_response)

    async def check(self) -> tuple[str, int | None, str | None]:
        """连通性检查 + 工具发现；返回 (health_status, latency_ms, error)。"""
        import time

        started = time.monotonic()
        try:
            tools = await self.list_tools()
            latency = int((time.monotonic() - started) * 1000)
            return "healthy", latency, f"发现 {len(tools)} 个工具"
        except Exception as exc:
            self.last_error = str(exc)[:500]
            return "unhealthy", None, self.last_error


class StdioMcpClient:
    """stdio 传输的最小实现（新行分隔 JSON-RPC）；仅供系统/管理员项。"""

    def __init__(self, *, command: str, args: list[str] | None = None, env: dict[str, str] | None = None,
                 timeout_seconds: float | None = None) -> None:
        self.command = command
        self.args = args or []
        self.env = env
        self.timeout = timeout_seconds or get_settings().mcp_client_timeout_seconds
        self._process: asyncio.subprocess.Process | None = None

    async def _start(self) -> None:
        if self._process is not None:
            return
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self.env,
            )
        except OSError as exc:
            raise ServiceUnavailableError(
                f"stdio 进程启动失败：{exc}", code="mcp_unreachable"
            ) from exc

    async def _request(self, message: dict[str, Any]) -> dict[str, Any] | None:
        await self._start()
        assert self._process is not None
        assert self._process.stdin is not None and self._process.stdout is not None
        request_id = message.get("id")
        self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self._process.stdin.drain()
        if request_id is None:
            return None
        while True:
            try:
                line = await asyncio.wait_for(self._process.stdout.readline(), timeout=self.timeout)
            except TimeoutError as exc:
                raise ServiceUnavailableError("stdio MCP 响应超时", code="mcp_timeout") from exc
            if not line:
                raise ServiceUnavailableError("stdio MCP 进程已退出", code="mcp_unreachable")
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("id") == request_id:
                return parsed

    async def initialize(self) -> None:
        result = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            }
        )
        if result is None or "result" not in result:
            raise ServiceUnavailableError("MCP initialize 失败", code="mcp_protocol_error")
        await self._request({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    async def list_tools(self) -> list[ToolInfo]:
        result = await self._request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        if result is None or "result" not in result:
            raise ServiceUnavailableError("MCP tools/list 失败", code="mcp_protocol_error")
        tools: list[ToolInfo] = []
        for item in result["result"].get("tools", []):
            if not isinstance(item, dict):
                continue
            tools.append(
                ToolInfo(
                    name=str(item.get("name") or "")[:128],
                    description=str(item.get("description") or "")[:4000],
                    input_schema=item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {},
                    annotations=item.get("annotations") if isinstance(item.get("annotations"), dict) else {},
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if result is None:
            raise ServiceUnavailableError("MCP tools/call 无响应", code="mcp_protocol_error")
        if "error" in result:
            raise ServiceUnavailableError(
                f"MCP 工具调用失败：{str(result['error'].get('message') or '未知错误')[:300]}",
                code="mcp_tool_error",
            )
        return _cap_size(result.get("result", {}), get_settings().mcp_max_response_bytes)

    async def close(self) -> None:
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except TimeoutError:
                self._process.kill()


def build_client(
    *,
    transport: str,
    url: str | None,
    command: str | None,
    args: list[str],
    headers: dict[str, str] | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> McpHttpClient | StdioMcpClient:
    """按服务器配置构造客户端；非法配置直接抛 422。"""
    if transport == "stdio":
        if not command:
            raise ValidationFailedError("stdio 传输必须提供 command")
        return StdioMcpClient(command=command, args=args)
    if not url:
        raise ValidationFailedError("远程传输必须提供 url")
    return McpHttpClient(
        url=url, transport=transport, headers=headers, http_transport=http_transport
    )
