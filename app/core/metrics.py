"""Prometheus 指标：独立 registry、有界标签与记录辅助函数。

指标只记录标识与状态，不记录用户内容、参数、密钥；标签值一律走白名单或短字符串。
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    GCCollector,
    Histogram,
    PlatformCollector,
    ProcessCollector,
    generate_latest,
)

from app import __version__
from app.core.config import Settings

REGISTRY = CollectorRegistry()

_HTTP_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)
_CHAT_DURATION_BUCKETS = (1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0)
_FIRST_TOKEN_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0)
_LLM_DURATION_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_TOOL_DURATION_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0)
_SEARCH_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)

BUILD_INFO = Gauge(
    "agent_hub_build_info",
    "构建信息；固定值 1，标签区分版本/环境/构建号",
    ["version", "env", "build"],
    registry=REGISTRY,
)
HTTP_REQUESTS_TOTAL = Counter(
    "agent_hub_http_requests_total",
    "HTTP 请求数",
    ["method", "route", "status"],
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "agent_hub_http_request_duration_seconds",
    "HTTP 请求耗时（到响应头写出）",
    ["method", "route"],
    buckets=_HTTP_DURATION_BUCKETS,
    registry=REGISTRY,
)
SSE_CONNECTIONS_ACTIVE = Gauge(
    "agent_hub_sse_connections_active",
    "当前打开的 SSE 连接数",
    registry=REGISTRY,
)
SSE_EVENTS_TOTAL = Counter(
    "agent_hub_sse_events_total",
    "SSE 事件发布数",
    ["event"],
    registry=REGISTRY,
)
CHAT_ACTIVE_RUNS = Gauge(
    "agent_hub_chat_active_runs",
    "注册表中活跃的 run 任务数",
    registry=REGISTRY,
)
CHAT_RUNS_TOTAL = Counter(
    "agent_hub_chat_runs_total",
    "终态 run 数",
    ["status"],
    registry=REGISTRY,
)
CHAT_RUN_DURATION = Histogram(
    "agent_hub_chat_run_duration_seconds",
    "run 总时长",
    ["status"],
    buckets=_CHAT_DURATION_BUCKETS,
    registry=REGISTRY,
)
CHAT_FIRST_TOKEN = Histogram(
    "agent_hub_chat_first_token_seconds",
    "首 token 时延",
    ["model"],
    buckets=_FIRST_TOKEN_BUCKETS,
    registry=REGISTRY,
)
LLM_CALLS_TOTAL = Counter(
    "agent_hub_llm_calls_total",
    "LLM 调用数",
    ["model", "status"],
    registry=REGISTRY,
)
LLM_CALL_DURATION = Histogram(
    "agent_hub_llm_call_duration_seconds",
    "LLM 调用时长",
    ["model"],
    buckets=_LLM_DURATION_BUCKETS,
    registry=REGISTRY,
)
LLM_TOKENS_TOTAL = Counter(
    "agent_hub_llm_tokens_total",
    "LLM token 数",
    ["model", "kind"],
    registry=REGISTRY,
)
TOOL_INVOCATIONS_TOTAL = Counter(
    "agent_hub_tool_invocations_total",
    "工具调用数",
    ["tool", "source", "status"],
    registry=REGISTRY,
)
TOOL_DURATION = Histogram(
    "agent_hub_tool_duration_seconds",
    "工具执行时长",
    ["tool", "source"],
    buckets=_TOOL_DURATION_BUCKETS,
    registry=REGISTRY,
)
A2A_TASKS_TOTAL = Counter(
    "agent_hub_a2a_tasks_total",
    "A2A 远程任务数",
    ["status"],
    registry=REGISTRY,
)
A2A_TASK_DURATION = Histogram(
    "agent_hub_a2a_task_duration_seconds",
    "A2A 远程任务时长",
    ["status"],
    buckets=_CHAT_DURATION_BUCKETS,
    registry=REGISTRY,
)
KNOWLEDGE_SEARCH_TOTAL = Counter(
    "agent_hub_knowledge_search_total",
    "知识库检索数",
    ["status"],
    registry=REGISTRY,
)
KNOWLEDGE_SEARCH_DURATION = Histogram(
    "agent_hub_knowledge_search_duration_seconds",
    "知识库检索时长",
    ["status"],
    buckets=_SEARCH_DURATION_BUCKETS,
    registry=REGISTRY,
)
KNOWLEDGE_SEARCH_HITS = Histogram(
    "agent_hub_knowledge_search_hits",
    "知识库检索命中条数",
    buckets=(0, 1, 2, 3, 5, 10, 20),
    registry=REGISTRY,
)
APPROVALS_TOTAL = Counter(
    "agent_hub_approvals_total",
    "审批决定数",
    ["decision"],
    registry=REGISTRY,
)
RATE_LIMITED_TOTAL = Counter(
    "agent_hub_rate_limited_total",
    "限流触发数",
    ["scope"],
    registry=REGISTRY,
)
CLEANUP_DELETED_TOTAL = Counter(
    "agent_hub_cleanup_deleted_total",
    "清理删除数",
    ["kind"],
    registry=REGISTRY,
)
CLEANUP_RUNS_TOTAL = Counter(
    "agent_hub_cleanup_runs_total",
    "清理任务运行数",
    ["status"],
    registry=REGISTRY,
)
STARTUP_RECOVERY_TOTAL = Counter(
    "agent_hub_startup_recovery_total",
    "启动/周期恢复处理数",
    ["kind"],
    registry=REGISTRY,
)

_BUILTIN_TOOLS = frozenset({"save_memory", "read_skill_file", "search_knowledge", "send_message"})
_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_setup_done = False
_ALL_METRICS = (
    BUILD_INFO,
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION,
    SSE_CONNECTIONS_ACTIVE,
    SSE_EVENTS_TOTAL,
    CHAT_ACTIVE_RUNS,
    CHAT_RUNS_TOTAL,
    CHAT_RUN_DURATION,
    CHAT_FIRST_TOKEN,
    LLM_CALLS_TOTAL,
    LLM_CALL_DURATION,
    LLM_TOKENS_TOTAL,
    TOOL_INVOCATIONS_TOTAL,
    TOOL_DURATION,
    A2A_TASKS_TOTAL,
    A2A_TASK_DURATION,
    KNOWLEDGE_SEARCH_TOTAL,
    KNOWLEDGE_SEARCH_DURATION,
    KNOWLEDGE_SEARCH_HITS,
    APPROVALS_TOTAL,
    RATE_LIMITED_TOTAL,
    CLEANUP_DELETED_TOTAL,
    CLEANUP_RUNS_TOTAL,
    STARTUP_RECOVERY_TOTAL,
)


def setup_metrics(settings: Settings) -> None:
    """注册进程/平台采集器并写入 build info；重复调用幂等。"""
    global _setup_done
    BUILD_INFO.labels(version=__version__, env=settings.env, build=settings.build_label).set(1)
    if _setup_done:
        return
    ProcessCollector(registry=REGISTRY)
    PlatformCollector(registry=REGISTRY)
    GCCollector(registry=REGISTRY)
    _setup_done = True


def reset_metrics() -> None:
    """测试辅助：清空所有本服务指标（保留采集器注册）。"""
    for metric in _ALL_METRICS:
        metric.clear()


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def _short(value: str, limit: int = 64) -> str:
    return str(value)[:limit]


def tool_metric_label(name: str, source: str) -> str:
    """工具标签白名单：原生工具保留名字，MCP/未知工具归一。"""
    if name in _BUILTIN_TOOLS:
        return name
    if source == "mcp" or name.startswith("mcp__"):
        return "mcp"
    return "other"


def record_http_request(
    method: str,
    route: str,
    status: int,
    duration_seconds: float,
    *,
    record_duration: bool = True,
) -> None:
    method_label = method.upper() if method.upper() in _HTTP_METHODS else "OTHER"
    HTTP_REQUESTS_TOTAL.labels(
        method=method_label, route=_short(route), status=str(status)
    ).inc()
    if record_duration:
        HTTP_REQUEST_DURATION.labels(method=method_label, route=_short(route)).observe(
            max(0.0, duration_seconds)
        )


def record_sse_connection(delta: int) -> None:
    SSE_CONNECTIONS_ACTIVE.inc(delta)


def record_sse_event(event: str) -> None:
    SSE_EVENTS_TOTAL.labels(event=_short(event)).inc()


def record_active_run(delta: int) -> None:
    CHAT_ACTIVE_RUNS.inc(delta)


def record_chat_run(
    status: str,
    duration_seconds: float,
    first_token_seconds: float | None,
    model: str,
) -> None:
    CHAT_RUNS_TOTAL.labels(status=status).inc()
    CHAT_RUN_DURATION.labels(status=status).observe(max(0.0, duration_seconds))
    if first_token_seconds is not None and first_token_seconds >= 0:
        CHAT_FIRST_TOKEN.labels(model=_short(model)).observe(first_token_seconds)


def record_llm_call(
    model: str,
    status: str,
    duration_seconds: float,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int,
) -> None:
    LLM_CALLS_TOTAL.labels(model=_short(model), status=status).inc()
    LLM_CALL_DURATION.labels(model=_short(model)).observe(max(0.0, duration_seconds))
    for kind, value in (
        ("input", input_tokens),
        ("output", output_tokens),
        ("reasoning", reasoning_tokens),
    ):
        if value:
            LLM_TOKENS_TOTAL.labels(model=_short(model), kind=kind).inc(value)


def record_tool_call(tool: str, source: str, status: str, duration_seconds: float) -> None:
    label = tool_metric_label(tool, source)
    TOOL_INVOCATIONS_TOTAL.labels(tool=label, source=_short(source), status=status).inc()
    TOOL_DURATION.labels(tool=label, source=_short(source)).observe(max(0.0, duration_seconds))


def record_a2a_task(status: str, duration_seconds: float) -> None:
    A2A_TASKS_TOTAL.labels(status=status).inc()
    A2A_TASK_DURATION.labels(status=status).observe(max(0.0, duration_seconds))


def record_knowledge_search(status: str, duration_seconds: float, hits: int) -> None:
    KNOWLEDGE_SEARCH_TOTAL.labels(status=status).inc()
    KNOWLEDGE_SEARCH_DURATION.labels(status=status).observe(max(0.0, duration_seconds))
    KNOWLEDGE_SEARCH_HITS.observe(max(0, hits))


def record_approval(decision: str) -> None:
    APPROVALS_TOTAL.labels(decision=decision).inc()


def record_rate_limited(scope: str) -> None:
    RATE_LIMITED_TOTAL.labels(scope=_short(scope)).inc()


def record_cleanup(kind: str, deleted: int) -> None:
    if deleted:
        CLEANUP_DELETED_TOTAL.labels(kind=kind).inc(deleted)


def record_cleanup_run(status: str) -> None:
    CLEANUP_RUNS_TOTAL.labels(status=status).inc()


def record_recovery(kind: str) -> None:
    STARTUP_RECOVERY_TOTAL.labels(kind=kind).inc()


__all__ = [
    "REGISTRY",
    "record_a2a_task",
    "record_active_run",
    "record_approval",
    "record_chat_run",
    "record_cleanup",
    "record_cleanup_run",
    "record_http_request",
    "record_knowledge_search",
    "record_llm_call",
    "record_rate_limited",
    "record_recovery",
    "record_sse_connection",
    "record_sse_event",
    "record_tool_call",
    "render_metrics",
    "reset_metrics",
    "setup_metrics",
    "tool_metric_label",
]
