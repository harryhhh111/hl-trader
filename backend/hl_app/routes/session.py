from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import (
    AuthError,
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthService,
    SessionInfo,
    authenticate_request,
    require_session,
)

router = APIRouter(prefix="/session")


class LoginBody(BaseModel):
    token: str = Field(min_length=1, max_length=256)


def _set_cookie(response: Response, request: Request, name: str, value: str, http_only: bool) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        name,
        value,
        max_age=settings.session_ttl_seconds,
        httponly=http_only,
        secure=settings.secure_cookie,
        samesite="lax",
        path="/",
    )


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    if request.app.state.settings.auth_mode != "token":
        raise HTTPException(400, "当前使用 Cloudflare Access 认证")
    auth: AuthService = request.app.state.auth
    try:
        credential = auth.login(body.token)
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    _set_cookie(response, request, SESSION_COOKIE, credential.session_token, True)
    _set_cookie(response, request, CSRF_COOKIE, credential.csrf_token, False)
    return {
        "data": {
            "authenticated": True,
            "expires_at": credential.expires_at,
            "auth_mode": "token",
        }
    }


@router.get("")
def session(request: Request) -> dict:
    auth_mode = request.app.state.settings.auth_mode
    try:
        info = authenticate_request(request)
    except AuthError as exc:
        if exc.status_code == 401:
            return {"data": {"authenticated": False, "auth_mode": auth_mode}}
        raise HTTPException(exc.status_code, exc.detail) from exc
    return {
        "data": {
            "authenticated": True,
            "expires_at": info.expires_at,
            "auth_mode": auth_mode,
            "email": info.email,
        }
    }


@router.post("/logout")
def logout(request: Request, response: Response, session: SessionInfo = Depends(require_session)) -> dict:
    if request.app.state.settings.auth_mode != "token":
        raise HTTPException(400, "请通过 Cloudflare Access 退出")
    request.app.state.auth.logout(session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"data": {"authenticated": False, "auth_mode": "token"}}
