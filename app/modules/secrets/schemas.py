"""平台密钥的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

SECRET_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class SecretOut(BaseModel):
    name: str
    description: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SecretListOut(BaseModel):
    items: list[SecretOut]


class SecretCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=SECRET_NAME_PATTERN)
    value: str = Field(min_length=1, max_length=4096, description="明文只用于写入，不回显")
    description: str = Field(default="", max_length=200)


class SecretUpdateRequest(BaseModel):
    value: str | None = Field(default=None, min_length=1, max_length=4096)
    description: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> SecretUpdateRequest:
        if self.value is None and self.description is None and self.enabled is None:
            raise ValueError("至少提供一个要修改的字段")
        return self
