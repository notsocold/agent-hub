"""指令模块：平台默认 + 用户覆盖（覆盖式）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

# 代码内置的默认指令；管理员可通过 admin 接口或 system_settings['instruction.default'] 覆盖。
DEFAULT_INSTRUCTION = (
    "回答使用中文；先给结论再给依据；专业问题给出信息来源或说明推断过程；"
    "不确定时明确说明，不要编造。"
)

SYSTEM_KEY_INSTRUCTION = "instruction.default"


class InstructionOut(BaseModel):
    effective: str
    source: str = Field(description="user：用户自定义；system_default：平台默认")
    user_value: str | None
    default_value: str
    max_chars: int
    updated_at: datetime


class InstructionUpdateRequest(BaseModel):
    content: str | None = Field(
        default=None,
        description="null 表示恢复平台默认；非空为整段覆盖，长度不得超过 max_chars",
    )

    @field_validator("content")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is not None:
            stripped = value.strip()
            return stripped or None
        return None


class InstructionUpdateResult(BaseModel):
    effective: str
    source: str
    max_chars: int
    updated_at: datetime
    request_id: str | None = None
