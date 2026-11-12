"""M9 步骤装配器纯函数：steps 校验、工具白名单、强制审批、事件映射与 prompt 渲染。

语义基准：spike 报告 §3.4（执行语义裁决）/§3.5（步骤 schema），
见 ``docs/superpowers/specs/2026-11-12-m9-spike-report.md``：

1. 白名单 = 各步骤允许工具的并集（严格白名单，基础工具不默认放行）；
2. ``approval`` 步骤 = 强制审批集合（``config.tools`` 里的工具一律走审批 gate），
   不是独立调度点，其 ``config.tools`` 也不进白名单；
3. ``workflow.step`` 事件按「工具名 → 步骤」映射（``step_for_tool``），
   ``approval``/``answer`` 步骤本身不提供工具、不产生事件。

全部为无状态纯函数，供 T3（CRUD 校验）与 T5/T6（chat 链路装配）消费。
"""

from __future__ import annotations

from typing import Any

# 步骤类型全集（spike §3.5；顺序即 schema 文档顺序）
STEP_TYPES: tuple[str, ...] = ("knowledge_search", "tool", "a2a", "approval", "answer")

# 类型默认提供的工具（spike §3.4 裁决 1：tool 取显式 config.tools，
# approval/answer 不提供工具，故不在此表）
TYPE_DEFAULT_TOOLS: dict[str, tuple[str, ...]] = {
    "knowledge_search": ("search_knowledge",),
    "a2a": ("send_message",),
}

# steps schema 约束上限（Global Constraints）
_MAX_STEPS = 12
_MAX_INSTRUCTION_LENGTH = 500
_MAX_TOOL_NAME_LENGTH = 200
_MAX_TOTAL_TEXT_LENGTH = 8000


def _provided_tools(step: dict[str, Any]) -> tuple[str, ...]:
    """单个步骤提供的工具名：类型默认工具或 tool 步骤的显式清单。

    ``approval``/``answer`` 恒为空——approval 的 ``config.tools`` 是强制审批
    标记而非工具授权（白名单语义见模块 docstring）。
    """
    step_type = step.get("type")
    if step_type == "tool":
        config = step.get("config")
        if isinstance(config, dict) and isinstance(config.get("tools"), list):
            return tuple(name for name in config["tools"] if isinstance(name, str))
        return ()
    return TYPE_DEFAULT_TOOLS.get(step_type, ())


def validate_steps(steps: Any) -> list[str]:
    """校验 steps 形状与全部约束，返回错误列表（空列表 = 合法）。

    约束（Global Constraints + 8000 总预算）：非空数组、≤12 步、
    ``type ∈ STEP_TYPES``、id 唯一非空、``instruction`` ≤500 字符、
    单工具名 ≤200 字符、tool 步骤必须显式非空 ``config.tools``、
    全部文本（拼接所有 id/title/instruction/config 工具名）总长 ≤8000 字符。
    收集全部错误不短路，便于上层一次报全。
    """
    if not isinstance(steps, list):
        return ["steps 必须是数组"]
    if not steps:
        return ["steps 不能为空数组"]

    errors: list[str] = []
    if len(steps) > _MAX_STEPS:
        errors.append(f"steps 数量 {len(steps)} 超过上限 {_MAX_STEPS}")

    seen_ids: set[str] = set()
    total_text = 0
    for index, step in enumerate(steps, start=1):
        label = f"第 {index} 步"
        if not isinstance(step, dict):
            errors.append(f"{label}必须是对象")
            continue

        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            errors.append(f"{label}缺少非空字符串 id")
        elif step_id in seen_ids:
            errors.append(f"{label} id 重复: {step_id}")
        else:
            seen_ids.add(step_id)

        step_type = step.get("type")
        if step_type not in STEP_TYPES:
            errors.append(f"{label} type 未知: {step_type!r}")

        title = step.get("title")
        if title is not None and not isinstance(title, str):
            errors.append(f"{label} title 必须是字符串")

        instruction = step.get("instruction")
        if instruction is not None:
            if not isinstance(instruction, str):
                errors.append(f"{label} instruction 必须是字符串")
            elif len(instruction) > _MAX_INSTRUCTION_LENGTH:
                errors.append(
                    f"{label} instruction 长度 {len(instruction)} 超过上限 {_MAX_INSTRUCTION_LENGTH}"
                )

        config = step.get("config")
        if config is not None and not isinstance(config, dict):
            errors.append(f"{label} config 必须是对象")
        config_tools: list[Any] | None = None
        if isinstance(config, dict) and isinstance(config.get("tools"), list):
            config_tools = config["tools"]

        if step_type == "tool":
            if not config_tools:
                errors.append(f"{label}（tool 步骤）必须显式提供非空 config.tools")
            else:
                for name in config_tools:
                    if not isinstance(name, str) or not name:
                        errors.append(f"{label} config.tools 必须是非空字符串列表")
                    elif len(name) > _MAX_TOOL_NAME_LENGTH:
                        errors.append(
                            f"{label} 工具名长度 {len(name)} 超过上限 {_MAX_TOOL_NAME_LENGTH}"
                        )

        # 总文本预算：拼接所有 id/title/instruction/config 工具名（仅计字符串值）
        if isinstance(step_id, str):
            total_text += len(step_id)
        if isinstance(title, str):
            total_text += len(title)
        if isinstance(instruction, str):
            total_text += len(instruction)
        if config_tools is not None:
            total_text += sum(len(name) for name in config_tools if isinstance(name, str))

    if total_text > _MAX_TOTAL_TEXT_LENGTH:
        errors.append(
            f"steps 全部文本总长度 {total_text} 超过上限 {_MAX_TOTAL_TEXT_LENGTH}"
        )
    return errors


def tool_allowlist(steps: list[dict[str, Any]]) -> set[str]:
    """各步骤允许工具的并集；空白名单表示模板未提供任何工具。"""
    allowlist: set[str] = set()
    for step in steps:
        if isinstance(step, dict):
            allowlist.update(_provided_tools(step))
    return allowlist


def force_approval(steps: list[dict[str, Any]]) -> set[str]:
    """强制审批工具集合：仅 ``approval`` 步骤的 ``config.tools``。"""
    tools: set[str] = set()
    for step in steps:
        if not isinstance(step, dict) or step.get("type") != "approval":
            continue
        config = step.get("config")
        if isinstance(config, dict) and isinstance(config.get("tools"), list):
            tools.update(name for name in config["tools"] if isinstance(name, str))
    return tools


def step_for_tool(steps: list[dict[str, Any]], tool_name: str) -> dict[str, Any] | None:
    """按模板顺序返回首个提供该工具的步骤（``workflow.step`` 事件映射）。

    ``approval``/``answer`` 步骤不提供工具，不会命中；未命中返回 None。
    """
    for step in steps:
        if isinstance(step, dict) and tool_name in _provided_tools(step):
            return step
    return None


def prompt_section(name: str, steps: list[dict[str, Any]]) -> str:
    """渲染注入 system prompt 的 workflow 段落。

    含「严格按序推进、禁止步骤外工具调用」约束句与每步骤一行
    （id / type / title / instruction / 可用工具）。
    """
    lines = [
        f'<workflow name="{name}">',
        (
            "工作流规则：严格按序推进以下步骤，完成一步后再进入下一步；"
            "禁止步骤外工具调用，只能使用当前步骤列出的工具。"
        ),
    ]
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            continue
        step_id = step.get("id") or f"step-{index}"
        parts = [f"{index}. [{step_id}] {step.get('type', '')}"]
        title = step.get("title")
        if isinstance(title, str) and title:
            parts.append(f"「{title}」")
        instruction = step.get("instruction")
        if isinstance(instruction, str) and instruction:
            parts.append(f"——{instruction}")
        provided = _provided_tools(step)
        if provided:
            parts.append(f"（可用工具：{'、'.join(provided)}）")
        lines.append("".join(parts))
    lines.append("</workflow>")
    return "\n".join(lines)


def filter_tools(tools: list[Any], allowlist: set[str]) -> list[Any]:
    """按 ``tool.name`` 过滤工具列表并保持顺序（返回新列表）。

    空白名单不过滤（防御坏模板），原样返回副本。
    """
    if not allowlist:
        return list(tools)
    return [tool for tool in tools if getattr(tool, "name", None) in allowlist]
