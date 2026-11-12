"""M9 步骤装配器纯函数测试：steps 校验、工具白名单、强制审批、事件映射与 prompt 渲染。

纯函数测试，不申请 ``db`` fixture（不连数据库）。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.modules.workflows.assembler import (
    STEP_TYPES,
    TYPE_DEFAULT_TOOLS,
    filter_tools,
    force_approval,
    prompt_section,
    step_for_tool,
    tool_allowlist,
    validate_steps,
)

THREE_STEPS = [
    {
        "id": "s1",
        "type": "knowledge_search",
        "title": "检索政策",
        "instruction": "先检索退货政策，找出可退换货的适用条件。",
    },
    {
        "id": "s2",
        "type": "a2a",
        "title": "委派远程",
        "instruction": "把该问题委派给客服代理处理。",
    },
    {
        "id": "s3",
        "type": "approval",
        "title": "人工确认",
        "config": {"tools": ["send_message"]},
    },
]


def test_step_types_and_default_tools() -> None:
    """类型清单与类型默认工具表与 spike §3.4/§3.5 裁决一致。"""
    assert set(STEP_TYPES) == {"knowledge_search", "tool", "a2a", "approval", "answer"}
    assert TYPE_DEFAULT_TOOLS == {
        "knowledge_search": ("search_knowledge",),
        "a2a": ("send_message",),
    }


def test_validate_steps_empty_array() -> None:
    """空数组与非数组输入（None/字符串）都非法。"""
    assert validate_steps([]) != []
    assert validate_steps(None) != []
    assert validate_steps("not-a-list") != []


def test_validate_steps_unknown_type() -> None:
    """type 不在 STEP_TYPES 内报错。"""
    steps = [{"id": "s1", "type": "bogus"}]
    assert validate_steps(steps) != []


def test_validate_steps_missing_id() -> None:
    """缺 id（或 id 非非空字符串）报错。"""
    assert validate_steps([{"type": "answer"}]) != []
    assert validate_steps([{"id": "", "type": "answer"}]) != []
    assert validate_steps([{"id": 42, "type": "answer"}]) != []


def test_validate_steps_duplicate_id() -> None:
    """id 重复报错。"""
    steps = [
        {"id": "s1", "type": "answer"},
        {"id": "s1", "type": "approval"},
    ]
    assert validate_steps(steps) != []


def test_validate_steps_tool_step_missing_tools() -> None:
    """tool 步骤必须显式提供非空 config.tools。"""
    assert validate_steps([{"id": "s1", "type": "tool"}]) != []
    assert validate_steps([{"id": "s1", "type": "tool", "config": {}}]) != []
    assert validate_steps([{"id": "s1", "type": "tool", "config": {"tools": []}}]) != []
    # 合法对照：显式非空工具清单
    assert validate_steps([{"id": "s1", "type": "tool", "config": {"tools": ["mcp__srv__t"]}}]) == []


def test_validate_steps_instruction_too_long() -> None:
    """instruction ≤500 字符，501 报错、500 合法（对照样例含提供工具的步骤）。"""
    # 合法对照：knowledge_search + answer（instruction 恰 500）
    assert validate_steps([
        {"id": "s1", "type": "knowledge_search"},
        {"id": "s2", "type": "answer", "instruction": "x" * 500},
    ]) == []
    # 超长：instruction 501 报错
    steps = [
        {"id": "s1", "type": "knowledge_search"},
        {"id": "s2", "type": "answer", "instruction": "x" * 501},
    ]
    assert validate_steps(steps) != []


def test_validate_steps_tool_name_too_long() -> None:
    """单工具名 ≤200 字符（Global Constraints），201 报错、200 合法。"""
    long_name = "t" * 201
    steps = [{"id": "s1", "type": "tool", "config": {"tools": [long_name]}}]
    assert validate_steps(steps) != []
    ok_name = "t" * 200
    ok_steps = [{"id": "s1", "type": "tool", "config": {"tools": [ok_name]}}]
    assert validate_steps(ok_steps) == []


def test_validate_steps_step_count_over_limit() -> None:
    """steps 超过 12 步报错；13 步触发上限（对照样例含提供工具的步骤）。"""
    steps = [{"id": f"s{i}", "type": "answer"} for i in range(1, 13)]
    steps.append({"id": "s13", "type": "knowledge_search"})
    assert validate_steps(steps) != []
    ok = [{"id": f"s{i}", "type": "answer"} for i in range(1, 12)]
    ok.append({"id": "s12", "type": "knowledge_search"})
    assert validate_steps(ok) == []


def test_validate_steps_length_budget() -> None:
    """steps 全部文本（id/title/instruction/config 工具名）总长 >8000 报错，8000 恰好合法。

    边界构造：s1 answer（仅 id + title）+ s2 knowledge_search（提供工具步骤），
    总长 = len("s1") + len(title) + len("s2")。
    """
    # 8000 恰好合法：2 + 7996 + 2 = 8000
    at_limit = [
        {"id": "s1", "type": "answer", "title": "t" * 7996},
        {"id": "s2", "type": "knowledge_search"},
    ]
    assert validate_steps(at_limit) == []
    # 8001 超预算：2 + 7997 + 2 = 8001，且其余约束均合法，错误应恰为预算一条
    over_limit = [
        {"id": "s1", "type": "answer", "title": "t" * 7997},
        {"id": "s2", "type": "knowledge_search"},
    ]
    errors = validate_steps(over_limit)
    assert len(errors) == 1
    assert "8000" in errors[0]


def test_validate_steps_requires_tool_provider() -> None:
    """终审裁决：合法模板必须至少含一个提供工具的步骤（防空白名单静默放行）。

    提供工具的步骤：knowledge_search / a2a（类型默认工具）、tool（显式
    config.tools）、approval 带非空 config.tools（终审裁决计入）。
    """
    # 纯 answer 模板：其余约束全合法 → 恰一条模板级错误，且不挂步骤前缀
    errors = validate_steps([{"id": "s1", "type": "answer"}])
    assert len(errors) == 1
    assert "提供工具的步骤" in errors[0]
    assert not errors[0].startswith("第")
    # 纯 approval（无 config.tools）报错
    assert validate_steps([{"id": "s1", "type": "approval"}]) != []
    # answer + knowledge_search 合法
    assert validate_steps([
        {"id": "s1", "type": "knowledge_search"},
        {"id": "s2", "type": "answer"},
    ]) == []
    # approval 带非空 config.tools 计入提供工具的步骤，合法
    assert validate_steps([
        {"id": "s1", "type": "approval", "config": {"tools": ["send_message"]}},
        {"id": "s2", "type": "answer"},
    ]) == []
    # tool 步骤（显式清单）计入，合法
    assert validate_steps([
        {"id": "s1", "type": "tool", "config": {"tools": ["mcp__srv__tool"]}},
    ]) == []


def test_validate_steps_legal_three_steps() -> None:
    """合法 THREE_STEPS 样例（同 spike：检索/a2a/approval）返回 []。"""
    assert validate_steps(THREE_STEPS) == []


def test_tool_allowlist_union() -> None:
    """白名单 = 各步骤允许工具的并集；approval 的 config.tools 不进白名单。"""
    assert tool_allowlist(THREE_STEPS) == {"search_knowledge", "send_message"}
    # tool 步骤贡献显式清单；approval 列出的 save_memory 是基础工具，严格白名单不放行
    steps = [
        *THREE_STEPS,
        {"id": "s4", "type": "tool", "config": {"tools": ["mcp__srv__tool"]}},
    ]
    assert tool_allowlist(steps) == {"search_knowledge", "send_message", "mcp__srv__tool"}


def test_force_approval_only_approval_steps() -> None:
    """强制审批集合仅来自 approval 步骤的 config.tools。"""
    assert force_approval(THREE_STEPS) == {"send_message"}
    steps = [
        {"id": "s1", "type": "knowledge_search"},
        {"id": "s2", "type": "tool", "config": {"tools": ["native_tool"]}},
        {"id": "s3", "type": "approval", "config": {"tools": ["send_message"]}},
    ]
    assert force_approval(steps) == {"send_message"}


def test_step_for_tool_first_match() -> None:
    """step_for_tool 按模板顺序首个命中；approval/answer 步骤不提供工具。"""
    assert step_for_tool(THREE_STEPS, "search_knowledge") == THREE_STEPS[0]
    # send_message 由 s2(a2a) 提供；s3(approval) 虽列出它但不算"提供"，不抢先命中
    assert step_for_tool(THREE_STEPS, "send_message") == THREE_STEPS[1]
    assert step_for_tool(THREE_STEPS, "save_memory") is None
    # tool 步骤按顺序首个命中
    steps = [
        {"id": "t1", "type": "tool", "config": {"tools": ["dup_tool"]}},
        {"id": "t2", "type": "tool", "config": {"tools": ["dup_tool"]}},
    ]
    assert step_for_tool(steps, "dup_tool")["id"] == "t1"


def test_filter_tools_filters_by_name() -> None:
    """filter_tools 按 tool.name 过滤并保持顺序，返回新列表。"""
    tools = [
        SimpleNamespace(name="search_knowledge"),
        SimpleNamespace(name="save_memory"),
        SimpleNamespace(name="send_message"),
    ]
    allowlist = {"search_knowledge", "send_message"}
    result = filter_tools(tools, allowlist)
    assert result == [tools[0], tools[2]]
    assert result is not tools


def test_filter_tools_empty_allowlist_passthrough() -> None:
    """空白名单不过滤（防御坏模板），原样返回内容相同的副本。"""
    tools = [SimpleNamespace(name="search_knowledge"), SimpleNamespace(name="send_message")]
    result = filter_tools(tools, set())
    assert result == tools
    assert result is not tools


def test_prompt_section_contains_workflow_and_steps() -> None:
    """prompt_section 渲染 <workflow name=…> 段落：约束句 + 每步一行（id/title/instruction）。"""
    section = prompt_section("退货处理流程", THREE_STEPS)
    assert '<workflow name="退货处理流程">' in section
    assert "</workflow>" in section
    # 约束句：严格按序推进、禁止步骤外工具调用
    assert "严格按序推进" in section
    assert "禁止步骤外工具调用" in section
    # 步骤行：含 id、title 与 instruction
    assert "[s1]" in section
    assert "检索政策" in section
    assert "先检索退货政策" in section
    assert "[s2]" in section
    assert "[s3]" in section
