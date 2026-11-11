"""部署产物静态校验（不启动容器）。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_non_root_and_healthcheck() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER app" in text
    assert "HEALTHCHECK" in text
    assert "--timeout-graceful-shutdown" in text
    assert "python:3.13-slim" in text


def test_compose_has_api_postgres_and_observability_profiles() -> None:
    text = (ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
    assert "pgvector/pgvector:pg16" in text
    assert "stop_grace_period: 40s" in text
    assert "profiles: [\"observability\"]" in text
    assert "prometheus" in text and "grafana" in text


def test_grafana_dashboard_is_valid_json_with_panels() -> None:
    data = json.loads(
        (ROOT / "deploy/grafana/dashboards/agent-hub-overview.json").read_text(encoding="utf-8")
    )
    assert data["title"]
    assert len(data["panels"]) >= 6
    assert any("histogram_quantile" in json.dumps(panel) for panel in data["panels"])


def test_nginx_disables_sse_buffering() -> None:
    text = (ROOT / "deploy/nginx.conf").read_text(encoding="utf-8")
    assert "proxy_buffering off" in text
    assert "proxy_read_timeout 900s" in text
