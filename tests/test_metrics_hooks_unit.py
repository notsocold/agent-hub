"""业务链路指标接入。"""

from __future__ import annotations

from app.core import metrics
from app.core.rate_limit import SlidingWindowLimiter


def test_rate_limit_metric(monkeypatch) -> None:
    metrics.reset_metrics()
    monkeypatch.setattr(
        "app.core.rate_limit.get_settings",
        lambda: type("S", (), {"rate_limit_enabled": True})(),
    )
    limiter = SlidingWindowLimiter()
    limiter.check("k", 1, 60, scope="chat")
    try:
        limiter.check("k", 1, 60, scope="chat")
    except Exception:
        pass
    text = metrics.render_metrics()[0].decode()
    assert 'agent_hub_rate_limited_total{scope="chat"} 1.0' in text


def test_active_run_gauge() -> None:
    metrics.reset_metrics()
    metrics.record_active_run(1)
    metrics.record_active_run(-1)
    text = metrics.render_metrics()[0].decode()
    assert "agent_hub_chat_active_runs 0.0" in text
