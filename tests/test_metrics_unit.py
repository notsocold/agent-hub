"""Prometheus 指标核心。"""

from __future__ import annotations

from app.core import metrics


def test_tool_label_whitelist() -> None:
    assert metrics.tool_metric_label("search_knowledge", "native") == "search_knowledge"
    assert metrics.tool_metric_label("save_memory", "native") == "save_memory"
    assert metrics.tool_metric_label("mcp__foo__bar", "mcp") == "mcp"
    assert metrics.tool_metric_label("weird", "native") == "other"


def test_metrics_render_and_reset() -> None:
    metrics.reset_metrics()
    metrics.record_http_request("GET", "/healthz", 200, 0.01)
    body, content_type = metrics.render_metrics()
    text = body.decode()
    assert "agent_hub_http_requests_total" in text
    assert 'route="/healthz"' in text
    assert "text/plain" in content_type
    metrics.reset_metrics()
    assert "agent_hub_http_requests_total{" not in metrics.render_metrics()[0].decode()


def test_metric_labels_are_bounded() -> None:
    metrics.reset_metrics()
    metrics.record_tool_call("mcp__evil__tool", "mcp", "ok", 0.1)
    text = metrics.render_metrics()[0].decode()
    assert 'tool="mcp"' in text
    assert "evil" not in text


def test_first_token_none_is_ignored() -> None:
    metrics.reset_metrics()
    metrics.record_chat_run("succeeded", 2.0, None, "fake-chat")
    text = metrics.render_metrics()[0].decode()
    assert 'agent_hub_chat_runs_total{status="succeeded"} 1.0' in text
    assert "agent_hub_chat_first_token_seconds_count" not in text
