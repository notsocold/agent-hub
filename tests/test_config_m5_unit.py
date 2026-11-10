"""M5 配置项默认值与环境变量覆盖。

构造 ``Settings`` 时禁用 ``.env`` 并清空被测字段环境变量，断言代码默认值；
覆盖用例显式设置环境变量后再构造。
"""

from __future__ import annotations

from app.core.config import Settings

_M5_ENV_VARS = (
    "RATE_LIMIT_CHAT_MAX",
    "RATE_LIMIT_CHAT_WINDOW_SECONDS",
    "PROMPT_GUARD_ENABLED",
    "PROMPT_GUARD_REDACT_OUTPUT",
)


def _default_settings(monkeypatch) -> Settings:
    for name in _M5_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/x")


def test_m5_defaults(monkeypatch) -> None:
    settings = _default_settings(monkeypatch)
    assert settings.rate_limit_chat_max == 30
    assert settings.rate_limit_chat_window_seconds == 60
    assert settings.prompt_guard_enabled is True
    assert settings.prompt_guard_redact_output is True


def test_m5_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_CHAT_MAX", "7")
    monkeypatch.setenv("RATE_LIMIT_CHAT_WINDOW_SECONDS", "120")
    monkeypatch.setenv("PROMPT_GUARD_ENABLED", "false")
    monkeypatch.setenv("PROMPT_GUARD_REDACT_OUTPUT", "false")
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@localhost/x")
    assert settings.rate_limit_chat_max == 7
    assert settings.rate_limit_chat_window_seconds == 120
    assert settings.prompt_guard_enabled is False
    assert settings.prompt_guard_redact_output is False
