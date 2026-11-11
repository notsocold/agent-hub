# M6 生产化与可观测实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 agent-hub 从「功能可跑」推进到「可部署、可观测、可运维」：结构化日志、OTel trace、Prometheus 指标、就绪检查、镜像/Compose、优雅停机、启动恢复、数据清理、流式过滤、压测/故障演练与 CI。

**Architecture:** 新增 `app/core/{telemetry,metrics,observability,log_context,stream_filter}.py` 作为横切基础设施；业务模块只调用指标/span 辅助函数。启动恢复与清理放在 `app/modules/maintenance/`，由 lifespan 调度；运行期行为改动集中在 `chat/service.py`（chat span、流式过滤、孤儿 run 终态）与 `chat/registry.py`（排水/活跃 gauge）。部署产物放仓库根与 `deploy/`，不启动容器。

**Tech Stack:** FastAPI、SQLAlchemy 2.x async、LangGraph 1.2.x、PostgreSQL + pgvector、prometheus-client 0.26、opentelemetry-sdk/exporter-otlp-proto-http 1.45、pytest-asyncio、httpx、Docker Compose。

**Spec:** `docs/superpowers/specs/2026-11-11-m6-production-observability-design.md`

## Global Constraints

- Python `>=3.13`；ruff `line-length = 100`；注释、日志、用户可见错误消息一律中文。
- 测试零网络、零外部存储；不启动任何新容器（复用本机 `dev_postgres`）。
- 装依赖用 `UV_CACHE_DIR=.uv-cache uv add ...` 或 `uv sync`；跑测试用 `.venv/bin/python`。
- 每个任务结束时 `pytest -q` 必须全绿；新测试先失败后实现（TDD）。
- 错误走 `app/core/errors.py` 既有类 + `code` 参数，不自造信封；HTTP 统一 `{"error": {...}}`。
- 指标标签白名单：禁止 `user_id` / `run_id` / `conversation_id` / 任意 MCP 工具名进指标。
- 密钥、用户消息正文、工具参数绝不进日志字段、span 属性、指标标签。
- OTel 未启用时零调用、零网络；Prometheus 用独立 registry，测试间 `reset_metrics()`。
- 提交只在本地，不推送、不删分支（用户偏好）；一个任务一个提交。
- 部署前必须 `alembic upgrade head`；测试库用 `create_all`，不能替代迁移检查。

## Review Focus

1. **SIGTERM 期间的 SSE**：在跑 run 必须收到终态事件；排水窗口耗尽才取消，不能直接断流。
2. **流式密钥跨 chunk**：密钥在任意位置被切开都不能出现在 `message.delta` / 落库内容里；正常文本不被改写或明显延迟。
3. **清理与在线 run 互斥**：有活跃/待审批 run 的会话 checkpoint 绝不删除；清理后会话再有新消息，标记必须自动失效。
4. **可观测关闭态**：`OTEL_ENABLED=false`、`METRICS_ENABLED=false`、无 OTLP endpoint 时请求正常、测试不访问外网、日志不刷导出错误。
5. **启动恢复边界**：有未过期 pending 审批的 run 不被取消；孤儿 `running` run 必须写终态，会话能继续发消息。

---

### Task 1: 依赖、配置与测试基线

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/core/config.py`
- Modify: `.env.example`
- Modify: `tests/integration/test_secrets_api.py:86-101`
- Test: `tests/test_config_m6_unit.py`

**Interfaces:**
- Produces: `Settings.log_format: Literal["text","json"]`、`log_level: str`、`otel_enabled: bool`、`otel_exporter: Literal["otlp","console","none"]`、`otel_exporter_otlp_traces_endpoint: str`、`otel_service_name: str`、`otel_sample_ratio: float`、`otel_export_timeout_seconds: float`、`metrics_enabled: bool`、`metrics_path: str`、`metrics_token: str`、`readiness_check_migrations: bool`、`readiness_timeout_seconds: float`、`shutdown_grace_seconds: int`、`startup_recovery_enabled: bool`、`startup_recovery_max_runs: int`、`recovery_interval_seconds: int`、`recovery_grace_seconds: int`、`recovery_batch_size: int`、`cleanup_enabled: bool`、`cleanup_interval_seconds: int`、`cleanup_batch_size: int`、`checkpoint_retention_days: int`、`run_events_retention_days: int`、`audit_retention_days: int`、`refresh_token_retention_days: int`。
- 默认值严格按 Spec §6.1/§7.1/§8.1/§11.1/§12.1：`log_format="text"`、`log_level="INFO"`、`otel_enabled=False`、`otel_exporter="otlp"`、`otel_exporter_otlp_traces_endpoint="http://localhost:4318/v1/traces"`、`otel_sample_ratio=0.1`、`metrics_enabled=True`、`metrics_path="/metrics"`、`readiness_check_migrations=False`、`shutdown_grace_seconds=30`、`startup_recovery_max_runs=100`、`recovery_interval_seconds=300`、`recovery_grace_seconds=60`、`recovery_batch_size=100`、`cleanup_interval_seconds=3600`、`cleanup_batch_size=500`、`checkpoint_retention_days=30`、`run_events_retention_days=90`、`audit_retention_days=180`、`refresh_token_retention_days=30`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_config_m6_unit.py
from pydantic import ValidationError
import pytest
from app.core.config import Settings


def test_m6_defaults():
    s = Settings(database_url="postgresql+psycopg://u:p@localhost/x", _env_file=None)
    assert s.log_format == "text"
    assert s.log_level == "INFO"
    assert s.otel_enabled is False
    assert s.otel_exporter == "otlp"
    assert s.otel_sample_ratio == 0.1
    assert s.metrics_enabled is True
    assert s.metrics_path == "/metrics"
    assert s.readiness_check_migrations is False
    assert s.shutdown_grace_seconds == 30
    assert s.startup_recovery_max_runs == 100
    assert s.recovery_interval_seconds == 300
    assert s.cleanup_interval_seconds == 3600
    assert s.audit_retention_days == 180


def test_m6_rejects_bad_values():
    with pytest.raises(ValidationError):
        Settings(otel_sample_ratio=1.5, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(metrics_path="metrics", _env_file=None)
    with pytest.raises(ValidationError):
        Settings(cleanup_interval_seconds=0, _env_file=None)
    with pytest.raises(ValidationError):
        Settings(log_level="LOUD", _env_file=None)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_config_m6_unit.py -q`
Expected: FAIL `AttributeError` / `ValidationError`（字段不存在）。

- [ ] **Step 3: 加依赖与配置**

`pyproject.toml` dependencies 增加：

```toml
"prometheus-client>=0.26.0,<1.0",
"opentelemetry-sdk>=1.45.2,<2.0",
"opentelemetry-exporter-otlp-proto-http>=1.45.2,<2.0",
```

`app/core/config.py` 在 M5 配置块后追加 M6 配置段；加校验：

```python
@field_validator("log_level")
@classmethod
def _normalize_log_level(cls, value: str) -> str:
    level = value.strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("LOG_LEVEL 必须是 DEBUG/INFO/WARNING/ERROR/CRITICAL")
    return level

@field_validator("metrics_path")
@classmethod
def _validate_metrics_path(cls, value: str) -> str:
    path = value.strip()
    if not path.startswith("/") or path in {"/", "/healthz", "/readyz"}:
        raise ValueError("METRICS_PATH 必须是 / 开头的非保留路径")
    return path

@field_validator("otel_sample_ratio")
@classmethod
def _validate_sample_ratio(cls, value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError("OTEL_SAMPLE_RATIO 必须在 0 到 1 之间")
    return value

@field_validator("cleanup_interval_seconds")
@classmethod
def _validate_cleanup_interval(cls, value: int) -> int:
    if value <= 0:
        raise ValueError("CLEANUP_INTERVAL_SECONDS 必须大于 0")
    return value
```

`.env.example` 追加 M6 段（与字段同名大写，注释说明默认单实例、OTel 未配置 no-op、prod 建议 `LOG_FORMAT=json`）。

`tests/integration/test_secrets_api.py::test_missing_capability_key_503` 改为不依赖 `.env`：

```python
from app.core.config import Settings

monkeypatch.setattr(
    "app.core.ssrf.get_settings",
    lambda: Settings(capability_secret_key="", _env_file=None),
)
```

保留 `monkeypatch.delenv` 与 `reset_settings_cache()`。

- [ ] **Step 4: 同步锁文件并跑测试**

Run: `UV_CACHE_DIR=.uv-cache uv sync --group dev && .venv/bin/python -m pytest tests/test_config_m6_unit.py tests/integration/test_secrets_api.py -q`
Expected: PASS（含 secrets 用例）。

- [ ] **Step 5: 提交**

```bash
git add pyproject.toml uv.lock app/core/config.py .env.example tests/test_config_m6_unit.py tests/integration/test_secrets_api.py
git commit -m "feat: M6 配置项与可观测依赖"
```

---

### Task 2: 结构化日志与运行上下文

**Files:**
- Create: `app/core/log_context.py`
- Modify: `app/core/logging.py`
- Modify: `app/main.py:45`
- Modify: `app/modules/chat/service.py`（`execute_run`、`resume_after_decision`、`stream_run_events`）
- Test: `tests/test_logging_unit.py`

**Interfaces:**
- Produces:
  - `app.core.log_context.run_id_ctx: ContextVar[str]`、`conversation_id_ctx: ContextVar[str]`
  - `@contextmanager def bind_run_context(run_id: str | uuid.UUID, conversation_id: str | uuid.UUID | None = None) -> Iterator[None]`
  - `class JsonFormatter(logging.Formatter)`，构造参数 `service: str`、`env: str`、`version: str`
  - `def configure_logging(settings: Settings, *, force: bool = False) -> None`
  - `def reset_logging() -> None`（测试辅助）
- JSON 字段：`ts`、`level`、`logger`、`message`、`service`、`env`、`version`、`request_id`、`run_id`、`conversation_id`、`trace_id`、`span_id`、`exception`（有 `exc_info` 时）、白名单 `extra`（`event`、`tool`、`a2a_task_id`、`tool_call_id`、`status`、`duration_ms`）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_logging_unit.py
import json
import logging
import io

from app.core.log_context import bind_run_context, run_id_ctx
from app.core.logging import JsonFormatter


def _record(**extra):
    record = logging.LogRecord("agent_hub.test", logging.INFO, __file__, 1, "hello", (), None)
    record.request_id = "req-1"
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_fields_and_context():
    with bind_run_context("run-1", "conv-1"):
        payload = json.loads(JsonFormatter(service="agent-hub", env="test", version="0.1").format(_record()))
    assert payload["message"] == "hello"
    assert payload["request_id"] == "req-1"
    assert payload["run_id"] == "run-1"
    assert payload["conversation_id"] == "conv-1"
    assert payload["service"] == "agent-hub"
    assert payload["trace_id"] == "-"  # OTel 未启用


def test_json_formatter_ignores_unknown_extra():
    payload = json.loads(
        JsonFormatter(service="agent-hub", env="test", version="0.1").format(_record(secret="x"))
    )
    assert "secret" not in payload


def test_bind_run_context_resets():
    with bind_run_context("run-1"):
        assert run_id_ctx.get() == "run-1"
    assert run_id_ctx.get() == ""
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_logging_unit.py -q`
Expected: FAIL `ModuleNotFoundError: app.core.log_context`。

- [ ] **Step 3: 实现**

`log_context.py` 用 `contextvars` + `@contextmanager`，两个 token 在 `finally` reset；UUID 转 `str`。

`logging.py` 保留 `RequestIdFilter` 与 text 格式；`JsonFormatter` 用 `datetime.now(UTC).isoformat(timespec="milliseconds")`，trace/span 从 `opentelemetry.trace.get_current_span()` 读取，无效时 `"-"`；`configure_logging(settings, force=False)` 先 `root.handlers.clear()`（仅 `force`），设置 `LOG_FORMAT`、`LOG_LEVEL`，handler 加 `RequestIdFilter`；`reset_logging()` 清 handler 并恢复 `WARNING`。

`main.py` 改 `configure_logging(settings)`。

`chat/service.py`：
- `execute_run` 调用 `_stream_graph` 处包 `with bind_run_context(run_id, runtime.conversation_id):`；
- `resume_after_decision` 同理；
- `stream_run_events` 顶部 set token，现有 `finally` 中 reset（与 `bus.unsubscribe` 同层）。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_logging_unit.py tests/integration/test_chat.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/core/log_context.py app/core/logging.py app/main.py app/modules/chat/service.py tests/test_logging_unit.py
git commit -m "feat: M6 结构化日志与运行上下文"
```

---

### Task 3: OpenTelemetry 核心与 LLM 回调

**Files:**
- Create: `app/core/telemetry.py`
- Modify: `app/main.py`（lifespan 启停）
- Test: `tests/test_telemetry_unit.py`

**Interfaces:**
- Produces:
  - `configure_tracing(settings: Settings | None = None) -> None`
  - `async def shutdown_tracing(timeout: float = 5.0) -> None`
  - `def get_tracer(name: str = "agent_hub") -> trace.Tracer`
  - `@contextmanager def start_span(name: str, *, kind: trace.SpanKind = trace.SpanKind.INTERNAL, attributes: Mapping[str, Any] | None = None) -> Iterator[trace.Span]`
  - `def current_trace_id() -> str`、`def current_span_id() -> str`（无有效 span 返回 `"-"`）
  - `def extract_trace_context(headers: Mapping[str, str]) -> Context`
  - 测试辅助 `def _set_provider_for_tests(provider: TracerProvider | None) -> None`
- `configure_tracing` 只在 `OTEL_ENABLED=true` 时创建 `TracerProvider`；exporter 构造异常捕获后 warning + no-op；`OTEL_EXPORTER=none` 直接 no-op；模块级 `_provider` 供 `get_tracer` 使用，不调用全局 `trace.set_tracer_provider`（便于测试注入）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_telemetry_unit.py
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core import telemetry
from app.core.config import Settings


def test_disabled_is_noop():
    telemetry._set_provider_for_tests(None)
    telemetry.configure_tracing(Settings(otel_enabled=False, _env_file=None))
    with telemetry.start_span("x") as span:
        span.set_attribute("a", 1)
    assert telemetry.current_trace_id() == "-"


def test_in_memory_span_attributes():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry._set_provider_for_tests(provider)
    try:
        with telemetry.start_span("chat.run", attributes={"app.run_id": "r1"}) as span:
            span.set_attribute("app.status", "ok")
        spans = exporter.get_finished_spans()
        assert spans[0].name == "chat.run"
        assert spans[0].attributes["app.run_id"] == "r1"
        assert telemetry.current_trace_id() == "-"
    finally:
        telemetry._set_provider_for_tests(None)
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_telemetry_unit.py -q`
Expected: FAIL `ModuleNotFoundError`。

- [ ] **Step 3: 实现**

`telemetry.py`：
- `Resource.create({"service.name": settings.otel_service_name, "deployment.environment": settings.env, "service.version": __version__})`；
- sampler `ParentBased(TraceIdRatioBased(settings.otel_sample_ratio))`；
- `BatchSpanProcessor(exporter)`；`exporter=otlp` → `OTLPSpanExporter(endpoint=..., timeout=settings.otel_export_timeout_seconds)`；`console` → `ConsoleSpanExporter()`；
- `start_span` 用 `get_tracer().start_as_current_span(..., kind=kind, attributes=dict(attributes or {}))`，异常时 `record_exception` + `set_status(Status(StatusCode.ERROR))` 后 re-raise；
- `shutdown_tracing` 用 `asyncio.wait_for(asyncio.to_thread(provider.shutdown), timeout)`，异常只 warning。

`main.py` lifespan：`configure_tracing(settings)` 在 `configure_logging` 后；关闭时 `await shutdown_tracing()` 放在 `dispose_engine()` 前。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_telemetry_unit.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/core/telemetry.py app/main.py tests/test_telemetry_unit.py
git commit -m "feat: M6 OpenTelemetry 初始化与 span 辅助"
```

---

### Task 4: Prometheus 指标核心、`/metrics` 与 LLM 回调

**Files:**
- Create: `app/core/metrics.py`
- Create: `app/agents/callbacks.py`
- Create: `app/modules/system/metrics_router.py`
- Modify: `app/main.py`（include router + `setup_metrics`）
- Modify: `app/modules/chat/service.py`（`_stream_graph` config 加 callback）
- Test: `tests/test_metrics_unit.py`
- Test: `tests/integration/test_metrics_api.py`

**Interfaces:**
- Produces:
  - `REGISTRY: CollectorRegistry`
  - `def setup_metrics(settings: Settings) -> None`
  - `def reset_metrics() -> None`
  - `def render_metrics() -> tuple[bytes, str]`
  - 指标对象与记录函数：`record_http_request(method, route, status, duration_seconds)`、`record_sse_connection(delta: int)`、`record_sse_event(event: str)`、`record_active_run(delta: int)`、`record_chat_run(status, duration_seconds, first_token_seconds, model)`、`record_llm_call(model, status, duration_seconds, input_tokens, output_tokens, reasoning_tokens)`、`record_tool_call(tool, source, status, duration_seconds)`、`record_a2a_task(status, duration_seconds)`、`record_knowledge_search(status, duration_seconds, hits)`、`record_approval(decision)`、`record_rate_limited(scope)`、`record_cleanup(kind, deleted)`、`record_recovery(kind)`
  - `def tool_metric_label(name: str, source: str) -> str`（白名单：`save_memory`/`read_skill_file`/`search_knowledge`/`send_message`/`mcp`/`other`）
  - `class ObservabilityCallbackHandler(BaseCallbackHandler)`，构造 `model: str`
- `/metrics` 路由由 `app/main.py` 用 `app.add_api_route(settings.metrics_path, metrics_endpoint, methods=["GET"], include_in_schema=False)` 注册；`METRICS_ENABLED=false` 不注册。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_metrics_unit.py
from app.core import metrics


def test_tool_label_whitelist():
    assert metrics.tool_metric_label("search_knowledge", "native") == "search_knowledge"
    assert metrics.tool_metric_label("mcp__foo__bar", "mcp") == "mcp"
    assert metrics.tool_metric_label("weird", "native") == "other"


def test_metrics_render_and_reset():
    metrics.reset_metrics()
    metrics.record_http_request("GET", "/healthz", 200, 0.01)
    body, content_type = metrics.render_metrics()
    text = body.decode()
    assert "agent_hub_http_requests_total" in text
    assert "text/plain" in content_type
    metrics.reset_metrics()
    assert "agent_hub_http_requests_total" not in metrics.render_metrics()[0].decode()
```

```python
# tests/integration/test_metrics_api.py
async def test_metrics_endpoint_and_http_counter(client):
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert "agent_hub_" in response.text
    await client.get("/healthz")
    assert "agent_hub_http_requests_total" in (await client.get("/metrics")).text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_metrics_unit.py tests/integration/test_metrics_api.py -q`
Expected: FAIL import / 404。

- [ ] **Step 3: 实现**

`metrics.py`：独立 `CollectorRegistry()`；注册 `ProcessCollector`/`PlatformCollector`/`GCCollector`；`BUILD_INFO = Gauge("agent_hub_build_info", ..., ["version","env","build"], registry=REGISTRY)`；全部指标名/标签严格按 Spec §8.2；`reset_metrics()` 对 Counter/Histogram/Gauge 调 `.clear()`（`BUILD_INFO` 也 clear），不重复注册。

`callbacks.py`：`ObservabilityCallbackHandler` 的 `on_chat_model_start` 开 `llm.chat` span；`on_llm_end` 结束 span、记录时长/token/状态；`on_llm_error` 记录 `status="error"`。token 取值顺序：`llm_output["token_usage"]` → `generations[0].message.usage_metadata` → `message.response_metadata["token_usage"]`；缺失记 0。

`metrics_router.py`：`async def metrics_endpoint(request: Request) -> Response`，`METRICS_TOKEN` 非空时校验 `Authorization: Bearer`，失败抛 `UnauthorizedError`；返回 `Response(content=body, media_type=content_type)`。

`main.py`：`setup_metrics(settings)`；`if settings.metrics_enabled: app.add_api_route(...)`。

`chat/service.py::_stream_graph`：`config.setdefault("callbacks", []).append(ObservabilityCallbackHandler(runtime.model_name))`。
若集成测试证明 LangGraph 的 `config["callbacks"]` 未传到模型节点，改为在 `prepare_runtime` 返回前
`model = model.with_config(callbacks=[ObservabilityCallbackHandler(run.model)])`；二选一，以测试结果为准。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_metrics_unit.py tests/integration/test_metrics_api.py tests/integration/test_chat.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/core/metrics.py app/agents/callbacks.py app/modules/system/metrics_router.py app/main.py app/modules/chat/service.py tests/test_metrics_unit.py tests/integration/test_metrics_api.py
git commit -m "feat: M6 Prometheus 指标与 /metrics"
```

---

### Task 5: HTTP/SSE 观测中间件

**Files:**
- Create: `app/core/observability.py`
- Modify: `app/main.py`（中间件顺序）
- Test: `tests/test_observability_middleware_unit.py`

**Interfaces:**
- Produces: `class ObservabilityMiddleware`，构造 `app: ASGIApp, *, metrics_path: str`；纯 ASGI，仅处理 `scope["type"]=="http"`。
- 行为：
  - 开 `http.request` span（`SpanKind.SERVER`），从请求头 `extract_trace_context`；属性 `http.request.method`、`url.path`；
  - 在 `http.response.start` 时：记录 `http.response.status_code`、`http.route`（`scope["route"].path` 或 `unmatched`）、`app.request_id`（响应头 `X-Request-ID`，缺失则 `-`）；追加 `X-Trace-ID`（有效 trace 时）；`content-type` 以 `text/event-stream` 开头时 `record_sse_connection(+1)` 并打标记；
  - 非 SSE 请求在 `http.response.start` 记录 `record_http_request`；SSE 不记 HTTP 时长（由 run 指标覆盖）；`metrics_path` 不自测；
  - `http.response.body` 的 `more_body=False` 或连接结束时 `record_sse_connection(-1)`；
  - 异常时 span `record_exception`，gauge 不泄漏。
- `main.py` 顺序：先 `ObservabilityMiddleware`，再 `RequestContextMiddleware`，最后 `CORSMiddleware`（CORS 最外层）。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_observability_middleware_unit.py
import httpx
from fastapi import FastAPI

from app.core import metrics
from app.core.middleware import RequestContextMiddleware
from app.core.observability import ObservabilityMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(ObservabilityMiddleware, metrics_path="/metrics")
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


async def test_http_metric_and_trace_header():
    metrics.reset_metrics()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/ping", headers={"X-Request-ID": "req-9"})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-9"
    text = metrics.render_metrics()[0].decode()
    assert 'route="/ping"' in text
    assert 'status="200"' in text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_observability_middleware_unit.py -q`
Expected: FAIL import。

- [ ] **Step 3: 实现**

纯 ASGI `__call__`；`send` 包装为闭包；SSE 标记 + `try/finally` 保证减 gauge；span 用 `start_span(..., kind=SERVER)` 手动 `use_span` 不可行（context manager 需包整个 `await self.app`），直接 `tracer.start_span` + `trace.use_span(span, end_on_exit=True)` 包住调用；捕获异常。

`main.py` 调整 `add_middleware` 顺序（CORS 最后添加，确保最外层）。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_observability_middleware_unit.py tests/integration/test_metrics_api.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/core/observability.py app/main.py tests/test_observability_middleware_unit.py
git commit -m "feat: M6 HTTP/SSE 观测中间件"
```

---

### Task 6: 业务埋点（chat/tool/A2A/检索/审批/限流/事件）

**Files:**
- Modify: `app/modules/chat/service.py`
- Modify: `app/modules/chat/tools.py`
- Modify: `app/modules/chat/registry.py`
- Modify: `app/modules/chat/events.py`
- Modify: `app/modules/a2a/tool.py`
- Modify: `app/modules/knowledge/service.py`
- Modify: `app/modules/approvals/service.py`
- Modify: `app/core/rate_limit.py`
- Modify: `app/modules/auth/service.py`
- Test: `tests/test_metrics_hooks_unit.py`
- Test: `tests/integration/test_observability_chat.py`

**Interfaces:**
- `chat/service.py`：
  - `_stream_graph` 外层 `start_span("chat.run", attributes={...})`；
  - `_finalize_success` / `_finalize_canceled` / `_finalize_failed` 写终态成功后 `record_chat_run(status, duration/1000, first_token_ms/1000 if not None, model)`；
  - `cancel_interrupted_run` 在真正发终态后同样记录 `canceled`；
  - `stream_run_events` 外层 `start_span("sse.stream")`，结束时设 `app.sse.events`、`app.sse.duration_ms`。
- `chat/tools.py::_execute_one`：`start_span("tool.execute", attributes={"app.tool.name":..., "app.tool.source":...})`；`finally` 里 `record_tool_call(tool_metric_label(name, source), source, status, elapsed_ms/1000)`。
- `chat/registry.py`：`start()` 时 `record_active_run(+1)`（Task 4 增加 `CHAT_ACTIVE_RUNS` gauge 的辅助 `record_active_run(delta)`）；done callback 减 1；`shutdown()` 结束后确保 gauge 清零。
- `chat/events.py::RunEventPublisher.emit`：`record_sse_event(event)`。
- `a2a/tool.py::execute_send_message`：`start_span("a2a.send_message")`；`finally` 用 `runner.task.state`、`elapsed_ms` 调 `record_a2a_task`。
- `knowledge/service.py::search`：`start_span("knowledge.search")`；成功/空/异常三态 `record_knowledge_search`，hits 用 `len(results)`。
- `approvals/service.py`：decide 成功、`expire_pending` 置 expired、取消 pending 时 `record_approval`。
- `core/rate_limit.py::check` 增加 `scope: str = "general"` 参数；触发时 `record_rate_limited(scope)`；三个调用点传 `register` / `login` / `chat`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_metrics_hooks_unit.py
from app.core import metrics
from app.core.rate_limit import SlidingWindowLimiter


def test_rate_limit_metric(monkeypatch):
    metrics.reset_metrics()
    monkeypatch.setattr("app.core.rate_limit.get_settings", lambda: type("S", (), {"rate_limit_enabled": True})())
    limiter = SlidingWindowLimiter()
    limiter.check("k", 1, 60, scope="chat")
    try:
        limiter.check("k", 1, 60, scope="chat")
    except Exception:
        pass
    text = metrics.render_metrics()[0].decode()
    assert 'agent_hub_rate_limited_total{scope="chat"} 1' in text
```

```python
# tests/integration/test_observability_chat.py
import httpx

from app.core import metrics
from tests.conftest import auth_headers, collect_sse, register_user


async def test_chat_metrics(client: httpx.AsyncClient) -> None:
    metrics.reset_metrics()
    registered = await register_user(client)
    headers = auth_headers(registered["access_token"])
    await collect_sse(client, headers=headers, payload={"message": "指标测试"})
    text = metrics.render_metrics()[0].decode()
    assert 'agent_hub_chat_runs_total{status="succeeded"} 1.0' in text
    assert 'agent_hub_sse_events_total{event="message.delta"}' in text
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_metrics_hooks_unit.py tests/integration/test_observability_chat.py -q`
Expected: FAIL（`scope` 参数不存在 / 指标为 0）。

- [ ] **Step 3: 实现**

按 Interfaces 逐点接入；注意 `_finalize_*` 的幂等早退分支不能重复计数；`record_chat_run` 的 duration 用秒（`/1000`），`first_token_ms` 为 `None` 时传 `0` 并在指标层忽略（不 observe）。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_metrics_hooks_unit.py tests/integration/test_observability_chat.py tests/integration/test_chat_rate_limit.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/modules/chat app/modules/a2a/tool.py app/modules/knowledge/service.py app/modules/approvals/service.py app/core/rate_limit.py app/modules/auth/service.py tests/test_metrics_hooks_unit.py tests/integration/test_observability_chat.py
git commit -m "feat: M6 业务链路指标与 span"
```

---

### Task 7: 健康检查、迁移检查与数据库不可用映射

**Files:**
- Create: `app/core/migrations.py`
- Modify: `app/modules/system/health.py`
- Modify: `app/main.py`（`app.state.draining` 初始化/停机置位）
- Modify: `app/core/errors.py`（`OperationalError` handler）
- Test: `tests/integration/test_readyz.py`
- Test: `tests/test_db_unavailable_unit.py`

**Interfaces:**
- Produces:
  - `def expected_migration_head() -> str | None`（`lru_cache`；`Config("alembic.ini")` + `ScriptDirectory.from_config(...).get_current_head()`）
  - `async def readiness_components(*, timeout: float) -> list[dict[str, object]]`
  - `/healthz` 增加 `version`/`build`/`env`
  - `/readyz` 增加 `version`/`build`/`env`/`draining`；`components` 含 `database`/`migrations`/`checkpoint`/`storage`
  - `READINESS_CHECK_MIGRATIONS=true` 时比对 `alembic_version.version_num` 与 `expected_migration_head()`；不一致 → migrations error（关键）
  - `CHECKPOINT_BACKEND=postgres` 时 `SELECT to_regclass('langgraph.checkpoints')` 非空；memory → `skipped`
  - storage：local 检查 `data_dir` 与 `knowledge_dir` 存在可写；s3 → `skipped`
  - draining 或关键组件 error → 503；storage error → 200 + `degraded`
- `errors.py`：注册 `OperationalError` / 连接类 `DBAPIError` handler → 503 `database_unavailable`，日志含 `request_id`。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_readyz.py
async def test_readyz_components(client):
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    names = {item["name"] for item in body["components"]}
    assert {"database", "checkpoint", "storage"} <= names


async def test_readyz_draining_503(client):
    from app.main import app
    app.state.draining = True
    try:
        assert (await client.get("/readyz")).status_code == 503
    finally:
        app.state.draining = False
```

```python
# tests/test_db_unavailable_unit.py
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.errors import register_exception_handlers


def test_operational_error_maps_503():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom():
        raise OperationalError("SELECT 1", {}, Exception("down"))

    with TestClient(app) as client:
        response = client.get("/boom")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/integration/test_readyz.py tests/test_db_unavailable_unit.py -q`
Expected: FAIL（组件缺失 / 500）。

- [ ] **Step 3: 实现**

`health.py` 用 `asyncio.wait_for` 包每个检查；返回 `JSONResponse`。`main.py` lifespan 开始 `app.state.draining=False`，`finally` 第一行置 `True`。`errors.py` 在 `Exception` handler 之前注册具体异常 handler（FastAPI 按具体类匹配，顺序不敏感，但先写具体）。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/integration/test_readyz.py tests/test_db_unavailable_unit.py tests/integration/test_auth.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/core/migrations.py app/modules/system/health.py app/main.py app/core/errors.py tests/integration/test_readyz.py tests/test_db_unavailable_unit.py
git commit -m "feat: M6 就绪检查与数据库不可用映射"
```

---

### Task 8: 优雅停机

**Files:**
- Modify: `app/modules/chat/registry.py`（`drain`）
- Modify: `app/main.py`（lifespan 关闭顺序）
- Test: `tests/test_registry_drain_unit.py`

**Interfaces:**
- Produces: `async def RunRegistry.drain(self, timeout: float) -> bool`；轮询所有 `context.task`，`asyncio.wait(..., timeout=min(remaining, 1.0))`；无任务立即 True；deadline 到返回 False。
- lifespan 关闭顺序：`app.state.draining=True` → cancel sweeper/maintenance → `await registry.drain(settings.shutdown_grace_seconds)` → `await registry.shutdown()` → `await shutdown_tracing()` → `await dispose_engine()`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_registry_drain_unit.py
import asyncio
from app.modules.chat.registry import RunContext, RunRegistry


async def test_drain_waits_for_tasks():
    registry = RunRegistry()
    context = RunContext(run_id=__import__("uuid").uuid4(), publisher=None)  # type: ignore[arg-type]
    registry.start(context, asyncio.sleep(0.2))
    assert await registry.drain(1.0) is True


async def test_drain_timeout_returns_false():
    registry = RunRegistry()
    context = RunContext(run_id=__import__("uuid").uuid4(), publisher=None)  # type: ignore[arg-type]
    registry.start(context, asyncio.sleep(5))
    assert await registry.drain(0.2) is False
    await registry.shutdown()
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_registry_drain_unit.py -q`
Expected: FAIL `AttributeError: drain`。

- [ ] **Step 3: 实现**

`drain` 用 `asyncio.get_running_loop().time()` 计时；收集 `context.task` 非空未完成；`asyncio.wait`；deadline 后返回 False，不 cancel（由 `shutdown()` 负责）。`main.py` 调整 `finally` 顺序；`sweeper.cancel()` 后 `await gather`，再 drain。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_registry_drain_unit.py tests/integration/test_chat.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/modules/chat/registry.py app/main.py tests/test_registry_drain_unit.py
git commit -m "feat: M6 优雅停机排水"
```

---

### Task 9: 启动恢复与巡检

**Files:**
- Create: `app/modules/maintenance/__init__.py`
- Create: `app/modules/maintenance/recovery.py`
- Modify: `app/modules/chat/service.py`（`fail_orphaned_run`、`_finalize_failed` 参数、`schedule_resume(recovery=True)`、`resume_after_decision` 的 restart_lost 分支）
- Modify: `app/main.py`（启动恢复 + 恢复巡检任务）
- Test: `tests/integration/test_startup_recovery.py`

**Interfaces:**
- Produces:
  - `async def chat_service.fail_orphaned_run(run_id: uuid.UUID, *, code: str = "run_interrupted", message: str = "服务重启导致运行中断，请重试") -> bool`
  - `async def chat_service.schedule_resume(run_id: uuid.UUID, *, recovery: bool = False) -> bool`；`RunContext` 增加 `recovery: bool = False`
  - `async def resume_after_decision(context)`：`snapshot.next` 为空时，若 `context.recovery`：回滚 `running→interrupted`，再 `cancel_interrupted_run(reason="restart_lost")`；否则沿用 M5 回滚 + warning
  - `app.modules.maintenance.recovery`：
    - `@dataclass(frozen=True) class RecoveryStats: scanned:int; resumed:int; canceled:int; failed:int; skipped:int`
    - `async def recover(*, startup: bool) -> RecoveryStats`
    - `async def recovery_sweeper(interval_seconds: int, *, grace_seconds: int, batch_size: int) -> None`
- `recover` 逻辑：先 `expire_pending()`（startup 时）；查询 `running/queued`（排除 `registry.is_running`、周期模式下 `created_at < now-grace`）→ `fail_orphaned_run`；查询 `interrupted` → 有 pending 则 skip；无 pending 则 `schedule_resume(run_id, recovery=True)`；每类 `limit`。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_startup_recovery.py
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models.approval import Approval
from app.db.models.chat import Run, RunEvent
from app.db.models.conversation import Conversation, Message
from app.db.models.user import User
from app.db.session import get_session_factory
from app.modules.maintenance.recovery import recover


async def _seed_run(*, status: str, with_pending: bool = False) -> uuid.UUID:
    async with get_session_factory()() as db:
        user = await db.scalar(select(User).limit(1))
        assert user is not None
        conversation = Conversation(user_id=user.id, title="恢复测试")
        db.add(conversation)
        await db.flush()
        user_message = Message(conversation_id=conversation.id, role="user", content="hi")
        assistant = Message(
            conversation_id=conversation.id, role="assistant", content="", status="streaming"
        )
        db.add_all([user_message, assistant])
        await db.flush()
        run = Run(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=assistant.id,
            status=status,
            model="fake-chat",
        )
        db.add(run)
        await db.flush()
        if with_pending:
            db.add(
                Approval(
                    run_id=run.id,
                    tool_name="mcp__x__write",
                    status="pending",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
        await db.commit()
        return run.id


async def test_orphan_running_run_failed(db, user) -> None:
    run_id = await _seed_run(status="running")
    stats = await recover(startup=True)
    assert stats.failed == 1
    async with get_session_factory()() as session:
        run = await session.get(Run, run_id)
        assert run.status == "failed"
        assert run.error_code == "run_interrupted"
        event = await session.scalar(select(RunEvent).where(RunEvent.run_id == run_id))
        assert event is not None and event.payload["_event"] == "error"


async def test_interrupted_with_pending_approval_kept(db, user) -> None:
    run_id = await _seed_run(status="interrupted", with_pending=True)
    stats = await recover(startup=True)
    assert stats.skipped >= 1
    async with get_session_factory()() as session:
        assert (await session.get(Run, run_id)).status == "interrupted"
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/integration/test_startup_recovery.py -q`
Expected: FAIL import / 状态不变。

- [ ] **Step 3: 实现**

`fail_orphaned_run` 复用 `_finalize_failed`（新增 `code`/`message`/`retryable` 参数，默认保持 `llm_error` 行为），随后把该 run 的 `pending/approved` 审批置 `canceled`，写审计 `run.recovered_failed`。`recover` 用 `get_session_factory` 查候选，逐个处理；异常单项捕获。`main.py` 启动时 `await recover(startup=True)`；`recovery_interval_seconds>0` 时创建任务 `recovery_sweeper(...)`，关闭时取消。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/integration/test_startup_recovery.py tests/integration/test_hitl_interrupt.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/modules/maintenance app/modules/chat/service.py app/main.py tests/integration/test_startup_recovery.py
git commit -m "feat: M6 启动恢复与孤儿 run 终态"
```

---

### Task 10: 数据清理服务与 CLI

**Files:**
- Create: `app/modules/maintenance/cleanup.py`
- Create: `scripts/cleanup.py`
- Modify: `app/main.py`（清理调度）
- Test: `tests/integration/test_cleanup_jobs.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class CleanupResult: kind: str; deleted: int; status: str; detail: str = ""`
  - `async def cleanup_checkpoints(*, dry_run: bool = False, limit: int | None = None) -> CleanupResult`
  - `async def cleanup_run_events(...)`、`cleanup_audit_logs(...)`、`cleanup_attachments(...)`、`cleanup_refresh_tokens(...)`
  - `async def run_all(*, only: set[str] | None = None, dry_run: bool = False) -> list[CleanupResult]`
  - `async def cleanup_sweeper(interval_seconds: int) -> None`
- checkpoint 选择：`Conversation.updated_at < cutoff`、无 `queued/running/interrupted` run、`metadata_["checkpoint_cleaned_at"].astext.is_(None)` 或 `updated_at > cast(marker, DateTime)`；删 `adelete_thread(str(id))`；标记 update 显式 `updated_at=Conversation.updated_at`（保留原值）。
- run_events：`Run.finished_at < cutoff` 且终态；分批 `DELETE`。
- audit：`AuditLog.created_at < cutoff` 分批。
- attachments：`expires_at < now()`；路径必须在 `settings.attachment_dir.resolve()` 内；被 `Message.attachments.contains([str(id)])` 引用则软删，否则删行。
- refresh tokens：`expires_at < now()-retention` 或 `revoked_at < cutoff`。
- CLI `scripts/cleanup.py`：`--dry-run`、`--only checkpoints,run_events,audit,attachments,refresh_tokens`；输出表；`error` 存在时 exit 1。

- [ ] **Step 1: 写失败测试**

```python
# tests/integration/test_cleanup_jobs.py
async def test_cleanup_audit_and_run_events(db):
    # 造 200 天前 audit_logs 与 100 天前 finished run + run_events
    # monkeypatch settings 的保留天数或直接调用 cleanup_* 并断言旧行删除、新行保留
    ...


async def test_cleanup_checkpoint_skips_active_conversation(db, ...):
    # conversation.updated_at 设为 60 天前，但存在 running run
    # cleanup_checkpoints() 后 conversation.metadata_ 无标记
    ...
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/integration/test_cleanup_jobs.py -q`
Expected: FAIL import。

- [ ] **Step 3: 实现**

按 Interfaces；`dry_run` 只 `select count`；每类独立 session；删除数 > 0 时 `write_audit` + `record_cleanup`；`run_all` 捕获单项异常返回 `status="error"`。`main.py` 在 `CLEANUP_ENABLED` 时启动 `cleanup_sweeper`。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/integration/test_cleanup_jobs.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/modules/maintenance/cleanup.py scripts/cleanup.py app/main.py tests/integration/test_cleanup_jobs.py
git commit -m "feat: M6 数据生命周期清理"
```

---

### Task 11: 流式输出实时过滤

**Files:**
- Create: `app/core/stream_filter.py`
- Modify: `app/modules/chat/service.py::_stream_graph`
- Test: `tests/test_stream_filter_unit.py`
- Test: `tests/integration/test_observability_chat.py`（追加跨 chunk 用例）

**Interfaces:**
- Produces: `class StreamingOutputFilter`：
  - `__init__(self, *, enabled: bool = True) -> None`
  - `@property def rules(self) -> set[str]`
  - `def feed(self, text: str) -> str`
  - `def flush(self) -> str`
- 行为：按 Spec §13.2；`sanitize_output` 对可输出前缀兜底；`flush` 清空；`MAX_BLOCK=16*1024`、`MAX_TAG=2*1024`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_stream_filter_unit.py
import pytest
from app.core.stream_filter import StreamingOutputFilter


@pytest.mark.parametrize("cut", range(1, 30))
def test_openai_key_split_never_leaks(cut):
    raw = "前缀 sk-abcdefghijklmnopqrstuvwxyz 后缀"
    f = StreamingOutputFilter()
    out = f.feed(raw[:cut]) + f.feed(raw[cut:]) + f.flush()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in out
    assert "***" in out


def test_normal_text_not_delayed():
    f = StreamingOutputFilter()
    assert f.feed("你好，世界") == "你好，世界"
    assert f.flush() == ""


def test_script_block_dropped_across_chunks():
    f = StreamingOutputFilter()
    out = f.feed("<scr") + f.feed("ipt>alert(1)</script>正文") + f.flush()
    assert "<script" not in out.lower()
    assert "alert(1)" not in out
    assert "正文" in out
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_stream_filter_unit.py -q`
Expected: FAIL import。

- [ ] **Step 3: 实现**

过滤器内部 `buffer: str`、`block_mode: str | None`、`rules: set[str]`；`feed` 追加后：
1. 若在 block mode，找 `</script>`/`</iframe>`/`</style>`；未闭合且长度超 `MAX_BLOCK` → 丢弃并记规则；闭合则丢弃整段后继续；
2. 检测新 block 起点（`<script`/`<iframe`/`<style`，大小写不敏感）；
3. 计算 hold 起点（未闭合 `<`；密钥锚点 + 字符集；锚点前缀）；
4. 输出 `sanitize_output(buffer[:hold])`，保留 `buffer[hold:]`；
5. `flush` 对剩余跑 `sanitize_output`。

`_stream_graph`：创建 `text_filter`/`reasoning_filter`；文本 chunk 先 `feed`，非空才 append `content_parts` 与发 `message.delta`；reasoning 同理；`graph.astream` 结束后、处理 snapshot 前 `flush` 两个 filter；`rules` 非空时写一次 `security.output_redacted` 审计（`detail={"phase":"stream","rules":...}`）。`PROMPT_GUARD_REDACT_OUTPUT=false` 时 filter `enabled=False`，直接透传。

- [ ] **Step 4: 跑测试**

Run: `.venv/bin/python -m pytest tests/test_stream_filter_unit.py tests/integration/test_observability_chat.py tests/integration/test_chat.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/core/stream_filter.py app/modules/chat/service.py tests/test_stream_filter_unit.py tests/integration/test_observability_chat.py
git commit -m "feat: M6 流式输出实时过滤"
```

---

### Task 12: 部署产物（Dockerfile / Compose / Grafana / Nginx）

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`
- Create: `docker-compose.prod.yml`
- Create: `deploy/prometheus/prometheus.yml`
- Create: `deploy/grafana/provisioning/datasources/datasource.yml`
- Create: `deploy/grafana/provisioning/dashboards/dashboards.yml`
- Create: `deploy/grafana/dashboards/agent-hub-overview.json`
- Create: `deploy/nginx.conf`
- Create: `deploy/README.md`
- Test: `tests/test_deploy_assets_unit.py`

**Interfaces:**
- Dockerfile：多阶段 `ghcr.io/astral-sh/uv:0.9.26` + `python:3.13-slim`；非 root `app`；`/app/data`；`HEALTHCHECK` 用 `python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"`；CMD 含 `--timeout-graceful-shutdown 90`。
- Compose：`postgres`（pgvector/pg16、healthcheck、volume）、`api`（build、env_file、depends_on healthy、`stop_grace_period: 160s`、volume、restart）；`prometheus`/`grafana` profile `observability`；`nginx` profile `proxy`。
- Grafana dashboard：JSON 可解析，至少 6 个 panel（HTTP RPS、HTTP P95、SSE active、chat P95、LLM tokens、tool errors），datasource 变量 `Prometheus`。
- Nginx：SSE `proxy_buffering off`、`proxy_read_timeout 900s`、`X-Accel-Buffering no`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_deploy_assets_unit.py
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_non_root_and_healthcheck():
    text = (ROOT / "Dockerfile").read_text()
    assert "USER app" in text
    assert "HEALTHCHECK" in text
    assert "--timeout-graceful-shutdown" in text


def test_grafana_dashboard_is_valid_json_with_panels():
    data = json.loads((ROOT / "deploy/grafana/dashboards/agent-hub-overview.json").read_text())
    assert data["title"]
    assert len(data["panels"]) >= 6
    assert any("histogram_quantile" in json.dumps(panel) for panel in data["panels"])
```

- [ ] **Step 2: 运行确认失败**

Run: `.venv/bin/python -m pytest tests/test_deploy_assets_unit.py -q`
Expected: FAIL file missing。

- [ ] **Step 3: 实现**

按 Spec §10 写产物；dashboard 的 PromQL 使用 `agent_hub_http_request_duration_seconds_bucket`、`agent_hub_chat_run_duration_seconds_bucket`、`agent_hub_chat_first_token_seconds_bucket`、`agent_hub_llm_tokens_total`、`agent_hub_tool_invocations_total{status="error"}`、`agent_hub_sse_connections_active`。Compose 中 `READINESS_CHECK_MIGRATIONS=true`、`LOG_FORMAT=json`、`OTEL_ENABLED=false`（默认）。

- [ ] **Step 4: 静态校验**

Run:
```bash
.venv/bin/python -m pytest tests/test_deploy_assets_unit.py -q
docker compose -f docker-compose.prod.yml config >/dev/null
```
Expected: PASS；compose config 无报错（不启动容器）。

- [ ] **Step 5: 提交**

```bash
git add Dockerfile .dockerignore docker-compose.prod.yml deploy tests/test_deploy_assets_unit.py
git commit -m "feat: M6 生产镜像与 Compose 编排"
```

---

### Task 13: 50 并发 SSE 压测脚本

**Files:**
- Create: `scripts/load_test_sse.py`
- Create: `scripts/README.md`（追加用法）

**Interfaces:**
- CLI：`--base-url http://127.0.0.1:8000`、`--concurrency 50`、`--rounds 1`、`--json-out PATH`、`--timeout 120`。
- 每个并发用户：注册（或 `--reuse-account` 用固定账号 + 独立会话）、创建 `POST /api/v1/conversations`、`POST /api/v1/chat/stream`，读到 `message.done`/`error`；记录 status、first_token（从 `message.delta` 首帧）、总时长。
- 前置：服务端 `RATE_LIMIT_ENABLED=false`、`LLM_PROVIDER=fake`；脚本不修改服务端配置。
- 输出：`ok/error` 计数、错误率、P50/P95/P99（first token、总时长）、状态码分布；`--json-out` 写 JSON。

- [ ] **Step 1: 写脚本骨架与 `--self-check`**（不依赖服务端）：`python scripts/load_test_sse.py --self-check` 打印参数并 exit 0；先让脚本可导入、可 `--help`。
- [ ] **Step 2: 实现并发逻辑**（asyncio + httpx；每个用户独立 token；SSE 逐行解析 `event:`/`data:`）。
- [ ] **Step 3: 本机跑通**

```bash
# 终端 A
RATE_LIMIT_ENABLED=false LLM_PROVIDER=fake .venv/bin/python -m uvicorn app.main:app --port 8000
# 终端 B
.venv/bin/python scripts/load_test_sse.py --concurrency 5 --rounds 1
```
Expected: 5/5 ok；P95 有值。

- [ ] **Step 4: 50 并发正式采样**

Run: `.venv/bin/python scripts/load_test_sse.py --concurrency 50 --rounds 1 --json-out .tmp/m6-load.json`
Expected: 错误率 < 1%；记录 P95；JSON 写入。

- [ ] **Step 5: 提交**

```bash
git add scripts/load_test_sse.py scripts/README.md
git commit -m "feat: M6 50 并发 SSE 压测脚本"
```

---

### Task 14: 数据库故障与优雅停机演练脚本

**Files:**
- Create: `scripts/db_outage_drill.py`
- Create: `scripts/graceful_shutdown_drill.py`
- Modify: `scripts/README.md`

**Interfaces:**
- `db_outage_drill.py`：
  - `asyncio.start_server` 的 TCP 代理（`--listen-port 0`），把 `DATABASE_URL` 指向代理；
  - 启动 uvicorn 子进程（`sys.executable -m uvicorn app.main:app --port <port>`，环境继承 + `DATABASE_URL`/`CHECKPOINT_DATABASE_URL` 指向代理）；
  - 步骤：`/readyz` 200 → 关代理 → `/readyz` 503、`/api/v1/conversations` 503 `database_unavailable` → 开代理 → `/readyz` 200 → 聊天成功；
  - 输出 PASS/FAIL 与耗时；服务进程始终不重启。
- `graceful_shutdown_drill.py`：
  - 启动两个 uvicorn 实例（8101/8102，独立 `ATTACHMENT_DIR`/`KNOWLEDGE_DIR`，`RATE_LIMIT_ENABLED=false`、`LLM_PROVIDER=fake`、`FAKE_LLM_DELAY_MS=500`）；
  - 在 A 上发起 5 个长 SSE（fake delay 调大），等首帧后向 A 发 SIGTERM；同时向 B 持续发请求；
  - 断言 A 上 5 个 SSE 都收到 `message.done`，B 请求 0 失败；A 退出后重启，反向再来一轮；
  - 输出 PASS/FAIL。

- [ ] **Step 1: 实现 `db_outage_drill.py` 的代理与进程管理**
- [ ] **Step 2: 本机跑 DB 演练**

Run: `.venv/bin/python scripts/db_outage_drill.py`
Expected: PASS（故障中 503、恢复后 200）。

- [ ] **Step 3: 实现 `graceful_shutdown_drill.py`**
- [ ] **Step 4: 本机跑停机演练**

Run: `.venv/bin/python scripts/graceful_shutdown_drill.py`
Expected: PASS（在跑 SSE 全终态、另一实例 0 失败）。

- [ ] **Step 5: 提交**

```bash
git add scripts/db_outage_drill.py scripts/graceful_shutdown_drill.py scripts/README.md
git commit -m "test: M6 数据库故障与优雅停机演练脚本"
```

---

### Task 15: CI、文档、OpenAPI、路线图与交付报告

**Files:**
- Create: `.github/workflows/ci.yml`
- Create: `docs/observability.md`
- Create: `docs/deployment.md`
- Create: `docs/feedback/2026-11-11-m6-backend-report.md`
- Modify: `README.md`、`docs/architecture.md`、`docs/development.md`、`docs/database.md`、`docs/roadmap.md`、`docs/openapi.json`

**Interfaces:**
- CI jobs：
  - `lint`：`uv sync --frozen --group dev` + `uv run ruff check .`
  - `test`：`pgvector/pgvector:pg16` service；创建 `agent_hub_ci` / `agent_hub_test` 两库 + `CREATE EXTENSION vector`；`TEST_DATABASE_URL=...agent_hub_test` 跑 `uv run pytest -q`
  - `migrations`：空库 `DATABASE_URL=...agent_hub_ci uv run alembic upgrade head` + `alembic check`
  - `contract`：`uv run python scripts/export_openapi.py && git diff --exit-code docs/openapi.json`
  - `docker`：`docker build -t agent-hub:ci .`
- 文档必须包含：日志字段表、指标目录、span 目录、PromQL、Compose/迁移/滚动发布/排水/清理/故障处理、证据表。
- 路线图 M6 全部勾选并写证据；交付报告含实测数字与未做项。

- [ ] **Step 1: 写 CI workflow**；本地用 `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` 校验 YAML。
- [ ] **Step 2: 写 `docs/observability.md` 与 `docs/deployment.md`**（按 Spec §18）。
- [ ] **Step 3: 更新架构/开发/数据库/README/路线图**；导出 OpenAPI。
- [ ] **Step 4: 全量验证**

```bash
.venv/bin/python -m ruff check .
TEST_DATABASE_URL=$(grep -E '^DATABASE_URL=' .env | cut -d= -f2- | sed 's#/agent_hub$#/agent_hub_test#') \
  .venv/bin/python -m pytest -q
.venv/bin/python scripts/export_openapi.py
git diff --exit-code docs/openapi.json
```
Expected: ruff 全绿；pytest 全绿（含新增用例）；OpenAPI 无未提交 diff。

- [ ] **Step 5: 写交付报告并提交**

```bash
git add .github docs README.md
git commit -m "docs: M6 交付报告、运维文档与 CI"
```

---

## Spec 覆盖对照

| Spec 章节 | 任务 |
|---|---|
| §6 结构化日志 | 2 |
| §7 OpenTelemetry | 3、4、6 |
| §8 Prometheus | 4、5、6 |
| §9 健康检查 | 7 |
| §10.1–10.2 镜像/Compose | 12 |
| §10.3 优雅停机 | 8、14 |
| §10.4 数据库不可用 | 7、14 |
| §11 数据生命周期 | 10 |
| §12 启动恢复 | 9 |
| §13 流式过滤 | 11 |
| §14 API 契约 | 7、15 |
| §15 测试与证据 | 各任务 + 13、14 |
| §16 风险 | 全局约束 + Review Focus |
| §18 文档 | 15 |

## 收尾：整分支审查

实施完成后：
1. 跑 `uv run ruff check .` + 全量 `pytest -q`；
2. 跑 50 并发压测、DB 故障演练、优雅停机演练，把数字写进交付报告；
3. 派独立子代理做整分支审查（重点 Review Focus 5 条），修复后复跑；
4. 汇报「已做 / 未做 / 证据 / 需要你操作的事项」，不推送远端、不删分支。
