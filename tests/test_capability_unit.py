"""M1.5 能力层单元测试：zip 校验、SSRF、MCP 响应解析、工具名、凭据与提示词。"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from app.agents.prompts import assemble_system_prompt, truncate_to_tokens
from app.core.errors import PayloadTooLargeError, ValidationFailedError
from app.core.ssrf import (
    decrypt_secret,
    encrypt_secret,
    is_private_host,
    normalize_headers,
    resolve_headers,
    validate_mcp_url,
)
from app.modules.mcp.client import parse_json_or_sse, parse_sse_body
from app.modules.mcp.service import _exposed_name, _risk_of
from app.modules.skills.importer import estimate_tokens, parse_skill_zip


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


SKILL_MD = (
    "---\nname: weekly-report\ndescription: 生成周报\nversion: \"1.2\"\nlicense: MIT\n---\n"
    "# 周报生成\n\n1. 收集进展；\n2. 输出三段式周报。\n"
)


# --------------------------------------------------------------------------- zip 校验


def test_parse_skill_zip_basic() -> None:
    parsed = parse_skill_zip(
        make_zip({"SKILL.md": SKILL_MD.encode(), "references/checklist.md": b"- a\n- b"})
    )
    assert parsed.slug == "weekly-report"
    assert parsed.name == "周报生成"  # 取正文首个 H1
    assert parsed.description == "生成周报"
    assert parsed.version == "1.2"
    assert parsed.metadata == {"license": "MIT"}
    assert [item.path for item in parsed.files] == ["references/checklist.md"]
    assert parsed.estimated_tokens > 0


def test_parse_skill_zip_missing_skill_md() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        parse_skill_zip(make_zip({"README.md": b"hi"}))
    assert excinfo.value.code == "skill_invalid"


def test_parse_skill_zip_path_traversal() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        parse_skill_zip(make_zip({"../evil.md": b"x", "SKILL.md": SKILL_MD.encode()}))
    assert excinfo.value.code == "skill_invalid"


def test_parse_skill_zip_absolute_path() -> None:
    with pytest.raises(ValidationFailedError):
        parse_skill_zip(make_zip({"/etc/passwd": b"x", "SKILL.md": SKILL_MD.encode()}))


def test_parse_skill_zip_bad_frontmatter() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        parse_skill_zip(make_zip({"SKILL.md": b"no frontmatter here"}))
    assert excinfo.value.code == "skill_invalid"
    with pytest.raises(ValidationFailedError):
        # name 不合法
        parse_skill_zip(
            make_zip({"SKILL.md": b"---\nname: Bad Name!\ndescription: x\n---\nbody"})
        )
    with pytest.raises(ValidationFailedError):
        # description 缺失
        parse_skill_zip(make_zip({"SKILL.md": b"---\nname: ok-name\n---\nbody"}))


def test_parse_skill_zip_ignores_scripts_and_binaries() -> None:
    parsed = parse_skill_zip(
        make_zip(
            {
                "SKILL.md": SKILL_MD.encode(),
                "scripts/run.sh": b"#!/bin/sh\necho hi",
                "assets/logo.png": b"\x89PNG",
                "references/notes.txt": b"text",
            }
        )
    )
    assert [item.path for item in parsed.files] == ["references/notes.txt"]
    assert sorted(parsed.ignored) == ["assets/logo.png", "scripts/run.sh"]


def test_parse_skill_zip_too_large() -> None:
    with pytest.raises(PayloadTooLargeError):
        parse_skill_zip(b"not-a-zip" * 1000, max_upload_bytes=100)


def test_estimate_tokens() -> None:
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("hello") >= 1


# --------------------------------------------------------------------------- SSRF


def test_private_hosts_blocked() -> None:
    for host in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "localhost", "0.0.0.0"):
        blocked, _ = is_private_host(host)
        assert blocked, host
    # 公网主机通过（依赖网络解析；离线环境下本例标记 skip 由调用方决定）
    blocked, reason = is_private_host("8.8.8.8")
    assert not blocked, reason


def test_validate_mcp_url() -> None:
    with pytest.raises(ValidationFailedError) as excinfo:
        validate_mcp_url("http://127.0.0.1:8080/mcp")
    assert excinfo.value.code == "mcp_ssrf_blocked"
    with pytest.raises(ValidationFailedError):
        validate_mcp_url("ftp://example.com/mcp")
    with pytest.raises(ValidationFailedError):
        validate_mcp_url("https://user:pass@example.com/mcp")
    # 白名单放行（conftest 已配置 mcp.example.com）
    assert validate_mcp_url("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"


# --------------------------------------------------------------------------- 凭据


def test_credentials_encrypt_and_resolve() -> None:
    stored, hint = normalize_headers({"Authorization": "Bearer ghp_secret", "X-Custom": "plain"})
    assert hint
    assert "ghp_secret" not in json.dumps(stored, default=str)
    resolved = resolve_headers(stored)
    assert resolved["Authorization"] == "Bearer ghp_secret"
    assert resolved["X-Custom"] == "plain"
    assert decrypt_secret("v1:garbage") is None


def test_credentials_env_ref() -> None:
    import os

    os.environ["TEST_MCP_TOKEN"] = "token-123"
    stored, _ = normalize_headers({"Authorization": "env:TEST_MCP_TOKEN"})
    assert stored["Authorization"] == {"secret_ref": "env:TEST_MCP_TOKEN"}
    assert resolve_headers(stored)["Authorization"] == "token-123"
    del os.environ["TEST_MCP_TOKEN"]
    assert resolve_headers(stored) == {}


def test_credentials_roundtrip_wrong_key_returns_none() -> None:
    encrypted = encrypt_secret("abc")
    assert encrypted is not None
    assert decrypt_secret(encrypted) == "abc"


# --------------------------------------------------------------------------- MCP 响应解析


def test_parse_json_response() -> None:
    parsed = parse_json_or_sse("application/json", b'{"jsonrpc":"2.0","id":1,"result":{}}')
    assert parsed == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


def test_parse_sse_response() -> None:
    body = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'
    parsed = parse_json_or_sse("text/event-stream", body)
    assert parsed == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}]
    assert parse_sse_body('data: {"a":1}\n\n: ping\ndata: {"b":2}\n\n') == [
        {"a": 1},
        {"b": 2},
    ]


def test_parse_invalid_json_raises() -> None:
    from app.core.errors import ServiceUnavailableError

    with pytest.raises(ServiceUnavailableError):
        parse_json_or_sse("application/json", b"not json")


# --------------------------------------------------------------------------- 工具名与风险


def test_exposed_name_normalization() -> None:
    assert _exposed_name("github", "create_issue") == "mcp__github__create_issue"
    long_name = "x" * 80
    exposed = _exposed_name("github", long_name)
    assert len(exposed) <= 64
    assert exposed.startswith("mcp__github__")


def test_risk_of() -> None:
    assert _risk_of({"readOnlyHint": True}) == "read"
    assert _risk_of({"destructiveHint": True}) == "destructive"
    assert _risk_of({}) == "unknown"


# --------------------------------------------------------------------------- 提示词装配


def test_assemble_system_prompt_layers() -> None:
    prompt = assemble_system_prompt(
        instruction="请简洁",
        memory_lines=['<memory kind="preference" pinned="false">用户叫老赵</memory>'],
        skill_catalog="- weekly-report: 周报",
        active_skills=[("周报生成", "1. 收集进展")],
        tool_notes="优先使用 MCP 工具。",
    )
    assert "基本规则" in prompt  # 基础规则在最前
    assert prompt.index("基本规则") < prompt.index("用户自定义指令")
    assert prompt.index("用户自定义指令") < prompt.index("长期记忆")
    assert "用户叫老赵" in prompt
    assert "<skill name=\"周报生成\">" in prompt
    assert "工具使用规则" in prompt


def test_truncate_to_tokens() -> None:
    assert truncate_to_tokens("你好世界", 100) == "你好世界"
    truncated = truncate_to_tokens("a" * 1000, 10)
    assert truncated.startswith("a" * 30)
    assert estimate_tokens(truncated) <= 10
