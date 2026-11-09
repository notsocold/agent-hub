"""认证模块请求/响应模型。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 255 or not EMAIL_PATTERN.match(email):
        raise ValueError("邮箱格式不正确")
    return email


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=64)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("展示名不能为空")
        return value

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("密码至少 8 个字符")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _normalize_email(value)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(min_length=10)


class UpdateMeRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)

    @field_validator("display_name")
    @classmethod
    def _display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("展示名不能为空")
        return value


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _new_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("新密码至少 8 个字符")
        return value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    status: Literal["active", "disabled"]
    role: Literal["user", "admin"]
    created_at: datetime
    last_login_at: datetime | None


class TokenResponse(BaseModel):
    user: UserOut
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
