"""本地 LangChain 模拟智能体集群：启动 A2A 服务并自动注册到 agent-hub。

一个进程内启动 N 个「用 LangChain 搭建的智能体」，每个智能体：

- 有独立的 Agent Card（``{public_url}{base_path}/{slug}/.well-known/agent-card.json``）；
- 讲 A2A 0.3 JSON-RPC：``message/send``、``message/stream``、``tasks/get``、
  ``tasks/cancel``，供 agent-hub 的出站客户端真实调用；
- 内部是 ``langchain.agents.create_agent``（系统提示词 + 可选工具 + ChatOpenAI），
  默认读项目 ``.env`` 的 DeepSeek 配置；``--fake-llm`` 可切到离线假模型；
- 服务就绪后脚本自动登录并 ``POST /api/v1/agents`` 注册进 agent-hub。

用法（先准备管理员账号与运行中的 agent-hub）：

.. code-block:: bash

    uv run python scripts/seed_dev_account.py --admin
    uv run python scripts/mock_langchain_agents.py            # 起服务 + 自动注册
    uv run python scripts/mock_langchain_agents.py --fake-llm # 离线跑通协议
    uv run python scripts/mock_langchain_agents.py --unregister

触发控制（写在任务文本里，脚本会剥离标记）：

- ``#slow``：延迟 ``--slow-seconds``（默认 3 秒）后执行；
- ``#fail``：直接返回 failed 任务，用于验证失败链路。

配置清单默认内置 5 个角色，可用 ``--config scripts/mock_agents.example.yaml``
加载 YAML/JSON 覆盖。脚本约定通过 ``app.core.config.get_settings()`` 读取环境变量，
不重复实现配置解析。

安全边界：服务默认只绑定 ``127.0.0.1``，且只用于本地开发联调；不带任何鉴权，
不要用 ``--host 0.0.0.0`` 暴露到公网。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import json
import logging
import operator
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langchain_openai import ChatOpenAI

from app.agents.fake_model import FakeStreamingChatModel
from app.core.config import Settings, get_settings

logger = logging.getLogger("mock-langchain-agents")

# 任务文本里的模拟控制指令；执行前剥离，避免污染提示词。
_CONTROL_RE = re.compile(r"#(slow|fail)\b", re.IGNORECASE)
# 每个 context 最多保留的历史消息条数（6 轮对话）。
_MAX_HISTORY_MESSAGES = 12
# 内存任务表上限，避免长时间运行无限增长。
_MAX_TASKS = 200
# 每个智能体保留的 context 数量上限，避免多轮会话无限累积。
_MAX_CONTEXTS = 100
_TASK_NOT_FOUND = -32001
_TASK_NOT_CANCELABLE = -32002
_METHOD_NOT_FOUND = -32601
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_INVALID_PARAMS = -32602
_TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}
# 与 agent-hub 注册接口、前端 Agent slug 规则保持一致。
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_SKILLS = 50


def _say(message: str = "") -> None:
    """控制台输出并立即刷新：脚本常被后台管道启动，默认 print 会块缓冲。"""
    print(message, flush=True)


# --------------------------------------------------------------------------- 智能体清单


@dataclass
class AgentSpec:
    """一个模拟智能体的静态定义（来自内置清单或 YAML/JSON）。"""

    slug: str
    name: str
    description: str
    prompt: str
    skills: list[dict[str, Any]] = field(default_factory=list)
    temperature: float = 0.3
    streaming: bool = True
    requires_approval: bool = False
    tools: list[str] = field(default_factory=list)
    model: str | None = None

    def card(self, *, url: str) -> dict[str, Any]:
        """生成 A2A 0.3 风格的 Agent Card。"""
        skills = self.skills or [
            {
                "id": self.slug,
                "name": self.name,
                "description": self.description,
                "tags": ["mock"],
                "examples": [],
            }
        ]
        rendered: list[dict[str, Any]] = []
        for skill in skills:
            item = {
                "id": str(skill.get("id") or self.slug),
                "name": str(skill.get("name") or self.name),
                "description": str(skill.get("description") or ""),
                "tags": [str(tag) for tag in (skill.get("tags") or [])],
                "examples": [str(example) for example in (skill.get("examples") or [])],
            }
            if self.requires_approval:
                item["metadata"] = {"requiresApproval": True}
            rendered.append(item)
        return {
            "protocolVersion": "0.3.0",
            "name": self.name,
            "description": self.description,
            "url": url,
            "version": "0.1.0",
            "capabilities": {"streaming": self.streaming, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": rendered,
        }


def default_agent_specs() -> list[AgentSpec]:
    """内置的 5 个角色，覆盖不同 skills，便于验证主图的工具选择。"""
    return [
        AgentSpec(
            slug="code-reviewer",
            name="代码审查助手",
            description="审查代码片段，指出缺陷、风险与可落地的改进建议",
            prompt=(
                "你是资深代码审查助手。收到代码片段或变更描述后，按「发现的问题 → 风险 → "
                "改进建议」输出中文审查意见；没有明显问题时明确说明。只输出审查结果，不要寒暄。"
            ),
            skills=[
                {
                    "id": "code_review",
                    "name": "代码审查",
                    "description": "审查 Python/TypeScript 等代码，给出问题与改进建议",
                    "tags": ["code", "review"],
                    "examples": ["帮我审查这段 Python 代码", "这段 SQL 有什么风险"],
                }
            ],
        ),
        AgentSpec(
            slug="data-analyst",
            name="数据分析助手",
            description="解释数据、计算指标并给出结论与统计口径说明",
            prompt=(
                "你是数据分析助手。根据用户给出的数据或问题，先说明统计口径与假设，再给计算"
                "过程和结论，最后列出需要注意的事项；数据不足时明确说明缺什么。用中文回答。"
            ),
            temperature=0.2,
            tools=["calculator"],
            skills=[
                {
                    "id": "data_analysis",
                    "name": "数据分析",
                    "description": "解释数据、计算指标、给出分析结论",
                    "tags": ["data", "analysis"],
                    "examples": ["这批订单数据说明了什么", "帮我算一下同比增幅"],
                }
            ],
        ),
        AgentSpec(
            slug="translator",
            name="翻译助手",
            description="中英互译，保留原文语气、术语与格式",
            prompt=(
                "你是中英翻译助手。中文输入译为地道英文，英文输入译为自然中文；保留原文语气、"
                "术语和格式；只输出译文，必要时在译文后用一行说明术语选择。"
            ),
            temperature=0.1,
            skills=[
                {
                    "id": "translate",
                    "name": "中英翻译",
                    "description": "中英互译，保留语气与术语",
                    "tags": ["translation", "language"],
                    "examples": ["把这段中文翻译成英文", "translate this paragraph"],
                }
            ],
        ),
        AgentSpec(
            slug="tech-writer",
            name="技术文档助手",
            description="撰写结构清晰的 README、接口说明与操作手册",
            prompt=(
                "你是技术文档助手。输出结构清晰的 Markdown（标题、步骤、示例、注意事项），"
                "面向开发者；避免空话，能给出命令或代码示例时就给出。用中文写作。"
            ),
            temperature=0.4,
            skills=[
                {
                    "id": "doc_writing",
                    "name": "技术文档写作",
                    "description": "写 README、接口说明、操作手册",
                    "tags": ["docs", "writing"],
                    "examples": ["帮我写这个脚本的 README", "整理一份部署操作手册"],
                }
            ],
        ),
        AgentSpec(
            slug="ops-troubleshooter",
            name="运维排障助手",
            description="根据故障现象给出可执行的排查步骤与修复建议",
            prompt=(
                "你是运维排障助手。按「现象 → 可能原因 → 排查步骤 → 修复建议」输出，步骤要可"
                "执行并带命令示例；不确定时给出验证方法而不是猜测。用中文回答。"
            ),
            temperature=0.2,
            skills=[
                {
                    "id": "troubleshooting",
                    "name": "故障排查",
                    "description": "根据现象给排查步骤与修复建议",
                    "tags": ["ops", "troubleshooting"],
                    "examples": ["服务 502 了怎么排查", "数据库连接池耗尽怎么办"],
                }
            ],
        ),
    ]


def _coerce_skills(raw: Any, fallback: dict[str, Any], slug: str) -> list[dict[str, Any]]:
    if raw is None:
        return [fallback]
    if not isinstance(raw, list):
        raise SystemExit(f"智能体「{slug}」的 skills 必须是列表")
    if len(raw) > _MAX_SKILLS:
        raise SystemExit(f"智能体「{slug}」的 skills 超过 {_MAX_SKILLS} 条上限")
    skills: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise SystemExit(f"智能体「{slug}」的 skills 条目必须是对象")
        skills.append(dict(item))
    return skills or [fallback]


def _require_bool(data: dict[str, Any], key: str, default: bool, slug: str) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise SystemExit(f"智能体「{slug}」的 {key} 必须是 true/false")
    return value


def _require_temperature(data: dict[str, Any], slug: str) -> float:
    raw = data.get("temperature", 0.3)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise SystemExit(f"智能体「{slug}」的 temperature 必须是数字")
    value = float(raw)
    if not 0 <= value <= 2:
        raise SystemExit(f"智能体「{slug}」的 temperature 必须在 0-2 之间")
    return value


def _require_tools(data: dict[str, Any], slug: str) -> list[str]:
    raw = data.get("tools")
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise SystemExit(f"智能体「{slug}」的 tools 必须是字符串列表，例如 [calculator]")
    unknown = [item for item in raw if item not in _TOOL_REGISTRY]
    if unknown:
        logger.warning(
            "智能体「%s」配置了未知工具 %s，已忽略（可用：%s）",
            slug,
            unknown,
            sorted(_TOOL_REGISTRY),
        )
    return [item for item in raw if item in _TOOL_REGISTRY]


def _spec_from_dict(data: dict[str, Any], index: int) -> AgentSpec:
    """把 YAML/JSON 中的一条记录规范成 AgentSpec；缺字段时给出可定位的错误。"""
    slug = str(data.get("slug") or "").strip()
    if not slug:
        raise SystemExit(f"智能体清单第 {index + 1} 条的 slug 不能为空")
    if not _SLUG_RE.match(slug):
        raise SystemExit(
            f"智能体 slug「{slug}」不合法：只允许小写字母、数字和连字符，且不能以连字符开头"
        )
    name = str(data.get("name") or slug).strip()
    description = str(data.get("description") or name).strip()
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise SystemExit(f"智能体「{slug}」缺少 prompt（系统提示词）")
    raw_model = data.get("model")
    if raw_model is not None and not isinstance(raw_model, str):
        raise SystemExit(f"智能体「{slug}」的 model 必须是字符串")
    fallback = {
        "id": slug,
        "name": name,
        "description": description,
        "tags": ["mock"],
        "examples": [],
    }
    return AgentSpec(
        slug=slug,
        name=name[:128],
        description=description[:2000],
        prompt=prompt,
        skills=_coerce_skills(data.get("skills"), fallback, slug),
        temperature=_require_temperature(data, slug),
        streaming=_require_bool(data, "streaming", True, slug),
        requires_approval=_require_bool(data, "requires_approval", False, slug),
        tools=_require_tools(data, slug),
        model=raw_model,
    )


def load_agent_specs(path: str | None) -> list[AgentSpec]:
    """加载智能体清单；``path`` 为空时返回内置 5 个角色。"""
    if not path:
        return default_agent_specs()
    config_path = Path(path)
    if not config_path.is_file():
        raise SystemExit(f"配置文件不存在：{path}")
    raw_text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - 环境缺 PyYAML 时给出明确提示
            raise SystemExit(
                "读取 YAML 清单需要 PyYAML（项目依赖链已包含）；也可改用 JSON 清单。"
            ) from exc
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise SystemExit(f"配置文件不是合法 YAML：{exc}") from exc
    else:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"配置文件不是合法 JSON：{exc}") from exc
    entries = data.get("agents") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"配置文件缺少非空 agents 列表：{path}")
    specs: list[AgentSpec] = []
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            raise SystemExit(f"智能体清单第 {index + 1} 条必须是对象")
        specs.append(_spec_from_dict(item, index))
    if not specs:
        raise SystemExit(f"配置文件没有可用的智能体定义：{path}")
    seen: set[str] = set()
    for spec in specs:
        if spec.slug in seen:
            raise SystemExit(f"智能体 slug 重复：{spec.slug}")
        seen.add(spec.slug)
    return specs


# --------------------------------------------------------------------------- 内置工具


_MATH_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if (isinstance(node, ast.Constant) and isinstance(node.value, int | float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _MATH_OPS:
        left = _safe_eval(node.left)
        right = _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("指数过大（上限 1000）")
        return _MATH_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("表达式只支持数字与 + - * / % ** 运算")


@tool
def calculator_tool(expression: str) -> str:
    """计算四则运算表达式，例如 ``(12-3)*4``。"""
    try:
        value = _safe_eval(ast.parse(expression, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError) as exc:
        return f"计算失败：{exc}"
    if value == int(value):
        return str(int(value))
    return f"{value:.10g}"


@tool
def current_time_tool() -> str:
    """返回当前 UTC 时间（ISO 8601）。"""
    return datetime.now(UTC).isoformat()


_TOOL_REGISTRY: dict[str, BaseTool] = {
    "calculator": calculator_tool,
    "current_time": current_time_tool,
}


# --------------------------------------------------------------------------- A2A 任务对象


@dataclass
class MockTask:
    """内存态 A2A 任务，供 ``tasks/get`` / ``tasks/cancel`` 查询。

    任务表是服务级共享的（本地 mock，按 agent 隔离没有实际收益）；
    ``tasks/cancel`` 通过 ``cancel_event`` 通知进行中的流式生成器停止发送终态。
    """

    id: str
    context_id: str
    state: str
    status_text: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    def status_message(self) -> dict[str, Any]:
        return {
            "role": "agent",
            "messageId": uuid.uuid4().hex,
            "parts": [{"kind": "text", "text": self.status_text}],
        }

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextId": self.context_id,
            "kind": "task",
            "status": {"state": self.state, "message": self.status_message()},
            "artifacts": self.artifacts,
        }


@dataclass
class AgentReply:
    """一次智能体执行结果：成功给 text，失败给 error。"""

    text: str
    state: str = "completed"
    error: str | None = None


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts") or []
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "\n".join(chunks).strip()


def _message_from_params(params: Any) -> dict[str, Any] | None:
    """校验 ``params.message`` 形状；非法返回 None，由调用方回 JSON-RPC -32602。"""
    if not isinstance(params, dict):
        return None
    message = params.get("message")
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    return message


def _content_text(content: Any) -> str:
    """把 LangChain 消息 content（str 或 content blocks 列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
        return "".join(chunks)
    return str(content or "")


# --------------------------------------------------------------------------- 智能体实现


class LangChainAgent:
    """一个用 LangChain 搭建、对外讲 A2A 协议的模拟智能体。"""

    def __init__(
        self,
        spec: AgentSpec,
        *,
        settings: Settings,
        use_fake_llm: bool,
        fake_delay_ms: int = 0,
        slow_seconds: float = 3.0,
    ) -> None:
        self.spec = spec
        self.settings = settings
        self.use_fake_llm = use_fake_llm
        self.fake_delay_ms = fake_delay_ms
        self.slow_seconds = slow_seconds
        self.model_name = "fake-chat" if use_fake_llm else (spec.model or settings.llm_model)
        self.model = self._build_model()
        tools = [_TOOL_REGISTRY[name] for name in spec.tools if name in _TOOL_REGISTRY]
        self._agent = create_agent(
            self.model,
            tools=tools,
            system_prompt=spec.prompt,
            name=f"mock-{spec.slug}",
        )
        # contextId → 历史消息，支持同一会话的多轮委派。
        self._history: dict[str, list[Any]] = {}

    def _build_model(self) -> BaseChatModel:
        if self.use_fake_llm:
            return FakeStreamingChatModel(
                model_name=self.model_name, delay_ms=self.fake_delay_ms
            )
        return ChatOpenAI(
            model=self.model_name,
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            temperature=self.spec.temperature,
            timeout=self.settings.llm_timeout_seconds,
            max_tokens=self.settings.llm_max_output_tokens,
            max_retries=1,
        )

    async def run(self, text: str, *, context_id: str) -> AgentReply:
        """执行一次任务；``#slow`` / ``#fail`` 控制模拟行为。"""
        controls = {item.lower() for item in _CONTROL_RE.findall(text)}
        cleaned = _CONTROL_RE.sub("", text).strip() or text
        history: list[Any] = []
        if context_id:
            history = self._history.setdefault(context_id, [])
            # 长时间运行时限制 context 数量：超出后丢弃最早建立的会话。
            while len(self._history) > _MAX_CONTEXTS:
                self._history.pop(next(iter(self._history)), None)
        if "slow" in controls:
            await asyncio.sleep(self.slow_seconds)
        if "fail" in controls:
            return AgentReply(text="", state="failed", error="模拟远程故障：任务被拒绝执行")
        try:
            result = await self._agent.ainvoke(
                {"messages": [*history, {"role": "user", "content": cleaned}]}
            )
        except Exception as exc:
            logger.exception("[%s] 执行失败", self.spec.slug)
            return AgentReply(text="", state="failed", error=f"{type(exc).__name__}: {exc}")
        messages = result.get("messages") or []
        reply_text = _content_text(getattr(messages[-1], "content", "")) if messages else ""
        if not reply_text.strip():
            reply_text = "（智能体没有返回文本内容）"
        if context_id:
            history.extend(
                [
                    {"role": "user", "content": cleaned},
                    {"role": "assistant", "content": reply_text},
                ]
            )
            del history[:-_MAX_HISTORY_MESSAGES]
        return AgentReply(text=reply_text)


# --------------------------------------------------------------------------- A2A ASGI 应用


class MockAgentCluster:
    """单进程承载多个智能体的 A2A 服务（每个智能体一个 URL path）。"""

    def __init__(
        self,
        specs: list[AgentSpec],
        *,
        public_url: str,
        base_path: str = "/agents",
        settings: Settings | None = None,
        use_fake_llm: bool = False,
        fake_delay_ms: int = 0,
        slow_seconds: float = 3.0,
    ) -> None:
        self.specs = {spec.slug: spec for spec in specs}
        self.public_url = public_url.rstrip("/")
        self.base_path = _normalize_base_path(base_path)
        self.settings = settings or get_settings()
        self.tasks: dict[str, MockTask] = {}
        self._agents = {
            spec.slug: LangChainAgent(
                spec,
                settings=self.settings,
                use_fake_llm=use_fake_llm,
                fake_delay_ms=fake_delay_ms,
                slow_seconds=slow_seconds,
            )
            for spec in specs
        }
        self.app = self._build_app()

    # ------------------------------------------------------------------ 对外信息

    def agent_url(self, slug: str) -> str:
        return f"{self.public_url}{self.base_path}/{slug}"

    def agent_urls(self) -> dict[str, str]:
        return _agent_urls(list(self.specs.values()), self.public_url, self.base_path)

    # ------------------------------------------------------------------ 任务表

    def _new_task(self, *, task_id: str, context_id: str, state: str, text: str) -> MockTask:
        task = MockTask(id=task_id, context_id=context_id, state=state, status_text=text)
        self.tasks[task.id] = task
        while len(self.tasks) > _MAX_TASKS:
            oldest = min(self.tasks.values(), key=lambda item: item.created_at)
            self.tasks.pop(oldest.id, None)
        return task

    def _result_artifact(self, slug: str, text: str) -> dict[str, Any]:
        return {
            "artifactId": uuid.uuid4().hex,
            "name": "result",
            "parts": [{"kind": "text", "text": text}],
            "metadata": {"type": "text", "agent": slug, "model": self._agents[slug].model_name},
        }

    # ------------------------------------------------------------------ JSON-RPC

    async def _dispatch(self, slug: str, payload: dict[str, Any]) -> JSONResponse:
        method = str(payload.get("method") or "")
        request_id = payload.get("id")
        params = payload.get("params") or {}
        agent = self._agents.get(slug)
        if agent is None:
            return _rpc_error(request_id, _METHOD_NOT_FOUND, f"unknown agent: {slug}")
        if method == "tasks/get":
            task_id = str((params.get("id") if isinstance(params, dict) else "") or "")
            task = self.tasks.get(task_id)
            if task is None:
                return _rpc_error(request_id, _TASK_NOT_FOUND, f"task not found: {task_id}")
            return _rpc_result(request_id, task.to_wire())
        if method == "tasks/cancel":
            task_id = str((params.get("id") if isinstance(params, dict) else "") or "")
            task = self.tasks.get(task_id)
            if task is None:
                return _rpc_error(request_id, _TASK_NOT_FOUND, f"task not found: {task_id}")
            if task.state in _TERMINAL_STATES:
                return _rpc_error(
                    request_id, _TASK_NOT_CANCELABLE, f"task not cancelable: {task.state}"
                )
            task.cancel_event.set()
            task.state = "canceled"
            task.status_text = "已取消"
            return _rpc_result(request_id, task.to_wire())
        if method == "message/send":
            message = _message_from_params(params)
            if message is None or not _message_text(message):
                return _rpc_error(
                    request_id,
                    _INVALID_PARAMS,
                    "params.message 必须是带非空 parts 文本列表的对象",
                )
            text = _message_text(message)
            context_id = str(message.get("contextId") or uuid.uuid4().hex)
            task_id = str(message.get("taskId") or f"task_{uuid.uuid4().hex[:12]}")
            started = time.monotonic()
            reply = await agent.run(text, context_id=context_id)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            task = self._new_task(
                task_id=task_id,
                context_id=context_id,
                state=reply.state,
                text="完成" if reply.state == "completed" else (reply.error or "失败"),
            )
            if reply.state == "completed":
                task.artifacts = [self._result_artifact(slug, reply.text)]
                logger.info(
                    "[%s] message/send 完成 task=%s %sms chars=%s",
                    slug,
                    task_id,
                    elapsed_ms,
                    len(reply.text),
                )
            else:
                logger.warning("[%s] message/send 失败 task=%s：%s", slug, task_id, reply.error)
            return _rpc_result(request_id, task.to_wire())
        return _rpc_error(request_id, _METHOD_NOT_FOUND, f"unknown method: {method}")

    async def _stream(self, slug: str, payload: dict[str, Any]) -> Any:
        """``message/stream``：working → artifact → completed 的 SSE 事件。"""
        request_id = payload.get("id")
        params = payload.get("params") or {}
        message = _message_from_params(params)
        if message is None or not _message_text(message):
            return _rpc_error(
                request_id,
                _INVALID_PARAMS,
                "params.message 必须是带非空 parts 文本列表的对象",
            )
        text = _message_text(message)
        context_id = str(message.get("contextId") or uuid.uuid4().hex)
        task_id = str(message.get("taskId") or f"task_{uuid.uuid4().hex[:12]}")
        agent = self._agents[slug]
        task = self._new_task(
            task_id=task_id, context_id=context_id, state="working", text="处理中…"
        )

        def frame(result: dict[str, Any]) -> str:
            body = {"jsonrpc": "2.0", "id": request_id, "result": result}
            return f"event: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"

        def status(state: str, text_value: str, *, final: bool) -> dict[str, Any]:
            return {
                "kind": "status-update",
                "taskId": task_id,
                "contextId": context_id,
                "status": {
                    "state": state,
                    "message": {
                        "role": "agent",
                        "messageId": uuid.uuid4().hex,
                        "parts": [{"kind": "text", "text": text_value}],
                    },
                },
                "final": final,
            }

        async def generate():
            try:
                if task.state in _TERMINAL_STATES:
                    return
                yield frame(status("working", "处理中…", final=False))
                started = time.monotonic()
                reply = await agent.run(text, context_id=context_id)
                elapsed_ms = int((time.monotonic() - started) * 1000)
                # 期间可能被 tasks/cancel 取消：不再发送任何终态，保持 canceled。
                if task.cancel_event.is_set() or task.state == "canceled":
                    logger.info("[%s] message/stream 已取消 task=%s", slug, task_id)
                    return
                if reply.state != "completed":
                    task.state = "failed"
                    task.status_text = reply.error or "失败"
                    logger.warning(
                        "[%s] message/stream 失败 task=%s：%s", slug, task_id, reply.error
                    )
                    yield frame(status("failed", task.status_text, final=True))
                    return
                artifact = self._result_artifact(slug, reply.text)
                task.artifacts = [artifact]
                task.state = "completed"
                task.status_text = "完成"
                logger.info(
                    "[%s] message/stream 完成 task=%s %sms chars=%s",
                    slug,
                    task_id,
                    elapsed_ms,
                    len(reply.text),
                )
                yield frame(
                    {
                        "kind": "artifact-update",
                        "taskId": task_id,
                        "contextId": context_id,
                        "artifact": artifact,
                    }
                )
                yield frame(status("completed", "完成", final=True))
            finally:
                # 客户端断开（生成器被取消）时任务不能永久停在 working。
                if task.state == "working":
                    task.state = "canceled"
                    task.status_text = "客户端断开"

        return StreamingResponse(generate(), media_type="text/event-stream")

    # ------------------------------------------------------------------ 路由

    def _build_app(self) -> FastAPI:
        app = FastAPI(title="Mock LangChain Agents", version="0.1.0")
        base = self.base_path

        @app.get("/healthz")
        async def healthz() -> dict[str, Any]:
            return {"status": "ok", "agents": sorted(self.specs)}

        @app.get("/")
        async def index() -> dict[str, Any]:
            return {
                "service": "mock-langchain-agents",
                "agents": [
                    {"slug": slug, "name": spec.name, "url": self.agent_url(slug)}
                    for slug, spec in self.specs.items()
                ],
            }

        @app.get(f"{base}/{{slug}}/.well-known/agent-card.json")
        @app.get(f"{base}/{{slug}}/.well-known/agent.json")
        async def agent_card(slug: str) -> JSONResponse:
            spec = self.specs.get(slug)
            if spec is None:
                return JSONResponse({"error": f"unknown agent: {slug}"}, status_code=404)
            return JSONResponse(spec.card(url=self.agent_url(slug)))

        @app.post(f"{base}/{{slug}}")
        async def jsonrpc(slug: str, request: Request) -> Any:
            if slug not in self._agents:
                return JSONResponse({"error": f"unknown agent: {slug}"}, status_code=404)
            try:
                payload = await request.json()
            except ValueError:
                return _rpc_error(None, _PARSE_ERROR, "JSON-RPC 请求不是合法 JSON")
            if not isinstance(payload, dict):
                return _rpc_error(None, _INVALID_REQUEST, "JSON-RPC 请求必须是对象")
            if payload.get("method") == "message/stream":
                return await self._stream(slug, payload)
            return await self._dispatch(slug, payload)

        return app


def _rpc_result(request_id: Any, result: Any) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


# --------------------------------------------------------------------------- 注册


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client: httpx.AsyncClient, email: str, password: str) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"登录失败（HTTP {response.status_code}）：{response.text[:300]}\n"
            "请先运行：uv run python scripts/seed_dev_account.py --admin"
        )
    return str(response.json()["access_token"])


async def _register_one(
    client: httpx.AsyncClient,
    token: str,
    *,
    slug: str,
    name: str,
    url: str,
) -> dict[str, Any] | None:
    try:
        response = await client.post(
            "/api/v1/agents",
            json={"url": url, "slug": slug, "name": name},
            headers=_auth_headers(token),
        )
    except httpx.HTTPError as exc:
        logger.warning("注册 %s 时无法连接 agent-hub：%s", slug, exc)
        return None
    if response.status_code == 201:
        return response.json()
    detail = response.text[:500]
    if response.status_code == 403:
        detail = "需要管理员权限；请先运行 seed_dev_account.py --admin"
    logger.error("注册 %s 失败（HTTP %s）：%s", slug, response.status_code, detail)
    return None


async def _health_check(client: httpx.AsyncClient, token: str, agent_id: str) -> dict[str, Any] | None:
    try:
        response = await client.post(
            f"/api/v1/agents/{agent_id}/health", headers=_auth_headers(token)
        )
    except httpx.HTTPError as exc:
        logger.warning("健康检查请求失败：%s", exc)
        return None
    if response.status_code != 200:
        logger.warning("健康检查返回 HTTP %s：%s", response.status_code, response.text[:300])
        return None
    return response.json()


async def _register_all(
    *,
    base_url: str,
    token: str,
    specs: list[AgentSpec],
    urls: dict[str, str],
    health_check: bool,
) -> int:
    failures = 0
    timeout = httpx.Timeout(60.0, connect=5.0)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False
    ) as client:
        for spec in specs:
            agent = await _register_one(
                client, token, slug=spec.slug, name=spec.name, url=urls[spec.slug]
            )
            if agent is None:
                failures += 1
                continue
            agent_id = str(agent["id"])
            _say(f"  ✅ {spec.slug:<18} id={agent_id}  url={urls[spec.slug]}")
            if health_check:
                health = await _health_check(client, token, agent_id)
                if health is not None:
                    status = health.get("health_status")
                    latency = health.get("latency_ms")
                    mark = "✅" if status == "healthy" else "⚠️"
                    _say(f"     {mark} 健康检查：{status}（{latency}ms）")
                    if status != "healthy":
                        _say(f"     ⚠️ {health.get('error') or '未达到 healthy'}")
    return failures


async def _unregister_all(
    *, base_url: str, token: str, specs: list[AgentSpec], urls: dict[str, str]
) -> int:
    """注销清单里的智能体：slug + URL 双匹配，避免误删用户自己注册的同名 Agent。"""
    targets = {spec.slug: urls[spec.slug] for spec in specs}
    removed = 0
    failures = 0
    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(
        base_url=base_url.rstrip("/"), timeout=timeout, trust_env=False
    ) as client:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            try:
                response = await client.get(
                    "/api/v1/agents", params=params, headers=_auth_headers(token)
                )
            except httpx.HTTPError as exc:
                logger.warning("查询 Agent 列表失败：%s", exc)
                return 1
            if response.status_code != 200:
                logger.error(
                    "查询 Agent 列表失败（HTTP %s）：%s",
                    response.status_code,
                    response.text[:300],
                )
                return 1
            try:
                data = response.json()
            except ValueError:
                logger.exception("Agent 列表不是合法 JSON：%s", response.text[:300])
                return 1
            for item in data.get("items") or []:
                target = targets.get(item.get("slug"))
                if target is None or item.get("url") != target:
                    continue
                try:
                    deleted = await client.delete(
                        f"/api/v1/agents/{item['id']}", headers=_auth_headers(token)
                    )
                except httpx.HTTPError as exc:
                    failures += 1
                    logger.warning("注销 %s 失败：%s", item.get("slug"), exc)
                    continue
                if deleted.status_code == 204:
                    removed += 1
                    _say(f"  🗑️  已注销 {item['slug']}（id={item['id']}）")
                else:
                    failures += 1
                    logger.warning(
                        "注销 %s 失败（HTTP %s）：%s",
                        item.get("slug"),
                        deleted.status_code,
                        deleted.text[:300],
                    )
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
    _say(f"共注销 {removed} 个智能体")
    if failures:
        _say(f"⚠️  {failures} 个注销失败（详见日志）")
        return 1
    return 0


# --------------------------------------------------------------------------- 启动


def _normalize_base_path(value: str) -> str:
    """URL 前缀归一化：空串/``/`` 表示不带前缀，避免拼出双斜杠。"""
    stripped = value.strip().strip("/")
    return f"/{stripped}" if stripped else ""


def _default_public_url(host: str, port: int) -> str:
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _agent_urls(specs: list[AgentSpec], public_url: str, base_path: str) -> dict[str, str]:
    """不依赖 LangChain 实例的注册 URL 计算（--register-only / --unregister 复用）。"""
    base = _normalize_base_path(base_path)
    root = public_url.rstrip("/")
    return {spec.slug: f"{root}{base}/{spec.slug}" for spec in specs}


async def _wait_ready(
    url: str,
    *,
    timeout: float = 10.0,
    server: uvicorn.Server | None = None,
    server_task: asyncio.Task | None = None,
) -> None:
    """等本进程的 uvicorn 真正完成启动，再探测 healthz。

    必须同时检查 ``server.started``：端口被其他进程占用时，healthz 可能由「别人」
    返回 200，只看 URL 会误判成启动成功。
    """
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            if server_task is not None and server_task.done():
                exc = None if server_task.cancelled() else server_task.exception()
                if isinstance(exc, SystemExit) or exc is None:
                    detail = "uvicorn 启动失败，请检查端口是否被占用"
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                raise RuntimeError(f"本地 A2A 服务启动失败：{detail}")
            if server is not None and not getattr(server, "started", False):
                await asyncio.sleep(0.1)
                continue
            try:
                response = await client.get(f"{url}/healthz")
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"本地 A2A 服务在 {timeout:g}s 内未就绪：{url}/healthz")


def _print_roster(specs: list[AgentSpec], urls: dict[str, str]) -> None:
    _say("\n可用的模拟智能体（已注册进 agent-hub 后可在聊天里这样委派）：")
    for spec in specs:
        _say(f"  - {spec.slug:<18} {spec.name}  {urls[spec.slug]}")
    first = specs[0].slug if specs else "<slug>"
    _say(
        f"\n聊天示例：请调用 send_message 工具，agent={first}，"
        "task 写「用一句话介绍你自己」\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="启动 LangChain 模拟智能体集群（A2A 服务）并自动注册到 agent-hub"
    )
    parser.add_argument("--config", help="智能体清单（YAML/JSON）；缺省用内置 5 个角色")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=9101, help="监听端口（默认 9101）")
    parser.add_argument(
        "--public-url",
        help="注册给 agent-hub 的对外地址（默认按 host/port 推导，代理场景可覆盖）",
    )
    parser.add_argument("--base-path", default="/agents", help="智能体 URL 前缀（默认 /agents）")
    parser.add_argument("--fake-llm", action="store_true", help="使用项目内置假模型，离线可跑")
    parser.add_argument("--slow-seconds", type=float, default=3.0, help="#slow 场景延迟秒数")
    parser.add_argument("--log-level", default="info", help="uvicorn 日志级别（默认 info）")
    parser.add_argument(
        "--agent-hub-url", default="http://127.0.0.1:8000", help="agent-hub 地址"
    )
    parser.add_argument("--email", default="dev@example.com", help="登录邮箱")
    parser.add_argument("--password", default="dev-password-123", help="登录密码")
    parser.add_argument("--token", help="直接使用现有 access token（跳过登录）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--no-register", action="store_true", help="只启动服务，不注册")
    mode.add_argument("--register-only", action="store_true", help="不启动服务，只注册")
    mode.add_argument("--unregister", action="store_true", help="注销清单里的智能体后退出")
    parser.add_argument(
        "--no-health-check", action="store_true", help="注册后不逐个做健康检查"
    )
    parser.add_argument("--dump-config", action="store_true", help="打印内置清单 YAML 后退出")
    return parser


def _dump_default_config(specs: list[AgentSpec]) -> None:
    payload = {"agents": []}
    for spec in specs:
        payload["agents"].append(
            {
                "slug": spec.slug,
                "name": spec.name,
                "description": spec.description,
                "prompt": spec.prompt,
                "skills": spec.skills,
                "temperature": spec.temperature,
                "streaming": spec.streaming,
                "requires_approval": spec.requires_approval,
                "tools": spec.tools,
                **({"model": spec.model} if spec.model else {}),
            }
        )
    try:
        import yaml
    except ImportError:  # pragma: no cover - 环境缺 PyYAML 时退化为 JSON
        _say(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    _say(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))


async def _resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return str(args.token)
    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(
        base_url=args.agent_hub_url.rstrip("/"), timeout=timeout, trust_env=False
    ) as client:
        return await _login(client, args.email, args.password)


async def _run(args: argparse.Namespace) -> int:
    if args.port <= 0 or args.port > 65535:
        raise SystemExit("--port 必须在 1-65535 之间")
    settings = get_settings()
    specs = load_agent_specs(args.config)
    if args.dump_config:
        _dump_default_config(specs)
        return 0

    public_url = args.public_url or _default_public_url(args.host, args.port)
    urls = _agent_urls(specs, public_url, args.base_path)
    need_token = args.unregister or args.register_only or not args.no_register
    token: str | None = None
    if need_token:
        try:
            token = await _resolve_token(args)
        except (httpx.HTTPError, RuntimeError):
            logger.exception("无法连接 agent-hub 或登录失败")
            if args.register_only or args.unregister:
                return 1
            _say("⚠️  注册已跳过；服务继续运行，修好 agent-hub 后可用 --register-only 补注册")

    if args.unregister:
        if token is None:
            return 1
        return await _unregister_all(
            base_url=args.agent_hub_url, token=token, specs=specs, urls=urls
        )

    if args.register_only:
        if token is None:
            return 1
        failures = await _register_all(
            base_url=args.agent_hub_url,
            token=token,
            specs=specs,
            urls=urls,
            health_check=not args.no_health_check,
        )
        _print_roster(specs, urls)
        return 1 if failures else 0

    # 只有真正要启动服务时才构造 LangChain/ChatOpenAI；注销、只注册不需要模型。
    use_fake = args.fake_llm or settings.is_fake_llm
    if use_fake and not args.fake_llm:
        _say("ℹ️  未配置 LLM_API_KEY 或 LLM_PROVIDER=fake，自动使用假模型（可用 --fake-llm 显式指定）")
    fake_delay = 0 if args.fake_llm else settings.fake_llm_delay_ms
    cluster = MockAgentCluster(
        specs,
        public_url=public_url,
        base_path=args.base_path,
        settings=settings,
        use_fake_llm=use_fake,
        fake_delay_ms=fake_delay,
        slow_seconds=args.slow_seconds,
    )

    config = uvicorn.Config(
        cluster.app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    local_host = "127.0.0.1" if args.host in {"0.0.0.0", "::", ""} else args.host
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    local_url = f"http://{local_host}:{args.port}"
    try:
        await _wait_ready(local_url, server=server, server_task=server_task)
        _say(f"\n🚀 本地 A2A 服务已启动：{public_url}{cluster.base_path}/<slug>")
        _say(f"   健康探针：{public_url}/healthz")
        model_desc = "离线假模型" if use_fake else f"真实 LLM（{settings.llm_model}）"
        _say(f"   模型模式：{model_desc}")
        if token is not None:
            _say(f"\n📌 自动注册到 {args.agent_hub_url}：")
            failures = await _register_all(
                base_url=args.agent_hub_url,
                token=token,
                specs=specs,
                urls=urls,
                health_check=not args.no_health_check,
            )
            if failures:
                _say(f"⚠️  {failures} 个智能体注册失败，其余仍可用")
            _print_roster(specs, urls)
        _say("服务运行中，Ctrl+C 退出（注册记录会保留；重新运行是幂等的）。\n")
        await server_task
    finally:
        server.should_exit = True
        if not server_task.done():
            server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, SystemExit):
            await server_task
    return 0


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if args.log_level.lower() == "info" else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        _say("\n已退出。")
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, int):
            _say(f"\n❌ 启动失败（退出码 {exc.code}），请检查端口是否被占用")
            return exc.code or 1
        _say(f"\n❌ {exc.code}")
        return 1
    except RuntimeError as exc:
        _say(f"\n❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
