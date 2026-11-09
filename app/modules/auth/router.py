"""认证路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.core.deps import CurrentUser, DbSession, client_ip, client_user_agent
from app.modules.auth import service
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateMeRequest,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, db: DbSession) -> TokenResponse:
    return await service.register(
        db,
        payload,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    return await service.login(
        db,
        payload,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, request: Request, db: DbSession) -> TokenResponse:
    return await service.refresh(
        db,
        payload.refresh_token,
        ip=client_ip(request),
        user_agent=client_user_agent(request),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: LogoutRequest, db: DbSession) -> None:
    await service.logout(db, payload.refresh_token)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
async def update_me(payload: UpdateMeRequest, user: CurrentUser, db: DbSession) -> UserOut:
    updated = await service.update_me(db, user, payload)
    return UserOut.model_validate(updated)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(payload: ChangePasswordRequest, user: CurrentUser, db: DbSession) -> None:
    await service.change_password(db, user, payload)
