"""M6 配置项默认值与校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://u:p@localhost/x",
        _env_file=None,
        **overrides,
    )


def test_m6_defaults() -> None:
    s = _settings()
    assert s.log_format == "text"
    assert s.log_level == "INFO"
    assert s.otel_enabled is False
    assert s.otel_exporter == "otlp"
    assert s.otel_exporter_otlp_traces_endpoint == "http://localhost:4318/v1/traces"
    assert s.otel_service_name == "agent-hub"
    assert s.otel_sample_ratio == 0.1
    assert s.metrics_enabled is True
    assert s.metrics_path == "/metrics"
    assert s.readiness_check_migrations is False
    assert s.readiness_timeout_seconds == 2.0
    assert s.shutdown_grace_seconds == 30
    assert s.startup_recovery_enabled is True
    assert s.startup_recovery_max_runs == 100
    assert s.recovery_interval_seconds == 300
    assert s.recovery_grace_seconds == 60
    assert s.recovery_batch_size == 100
    assert s.cleanup_enabled is True
    assert s.cleanup_interval_seconds == 3600
    assert s.cleanup_batch_size == 500
    assert s.checkpoint_retention_days == 30
    assert s.run_events_retention_days == 90
    assert s.audit_retention_days == 180
    assert s.refresh_token_retention_days == 30


def test_m6_normalizes_log_level() -> None:
    assert _settings(log_level="debug").log_level == "DEBUG"


def test_m6_rejects_invalid_values() -> None:
    with pytest.raises(ValidationError):
        _settings(otel_sample_ratio=1.5)
    with pytest.raises(ValidationError):
        _settings(metrics_path="metrics")
    with pytest.raises(ValidationError):
        _settings(metrics_path="/readyz")
    with pytest.raises(ValidationError):
        _settings(cleanup_interval_seconds=0)
    with pytest.raises(ValidationError):
        _settings(log_level="LOUD")
