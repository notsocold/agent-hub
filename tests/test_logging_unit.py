"""结构化日志与运行上下文。"""

from __future__ import annotations

import json
import logging

from app.core.log_context import bind_run_context, conversation_id_ctx, run_id_ctx
from app.core.logging import JsonFormatter


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "agent_hub.test", logging.INFO, __file__, 1, "hello", (), None
    )
    record.request_id = "req-1"
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _formatter() -> JsonFormatter:
    return JsonFormatter(service="agent-hub", env="test", version="0.1.0")


def test_json_formatter_fields_and_context() -> None:
    with bind_run_context("run-1", "conv-1"):
        payload = json.loads(_formatter().format(_record()))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "agent_hub.test"
    assert payload["message"] == "hello"
    assert payload["service"] == "agent-hub"
    assert payload["env"] == "test"
    assert payload["version"] == "0.1.0"
    assert payload["request_id"] == "req-1"
    assert payload["run_id"] == "run-1"
    assert payload["conversation_id"] == "conv-1"
    assert payload["trace_id"] == "-"
    assert payload["span_id"] == "-"


def test_json_formatter_ignores_unknown_extra() -> None:
    payload = json.loads(_formatter().format(_record(secret="x", status="ok")))
    assert "secret" not in payload
    assert payload["status"] == "ok"


def test_json_formatter_exception() -> None:
    try:
        int("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "agent_hub.test",
            logging.ERROR,
            __file__,
            1,
            "failed",
            (),
            sys.exc_info(),
        )
        record.request_id = "req-2"
        payload = json.loads(_formatter().format(record))
    assert "ValueError: invalid literal for int()" in payload["exception"]


def test_bind_run_context_resets() -> None:
    with bind_run_context("run-1", "conv-1"):
        assert run_id_ctx.get() == "run-1"
        assert conversation_id_ctx.get() == "conv-1"
    assert run_id_ctx.get() == ""
    assert conversation_id_ctx.get() == ""
