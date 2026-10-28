from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import (
    AuthError,
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthService,
    SessionInfo,
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
    auth: AuthService = request.app.state.auth
    try:
        credential = auth.login(body.token)
    except AuthError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    _set_cookie(response, request, SESSION_COOKIE, credential.session_token, True)
    _set_cookie(response, request, CSRF_COOKIE, credential.csrf_token, False)
    return {"data": {"authenticated": True, "expires_at": credential.expires_at}}


@router.get("")
def session(request: Request) -> dict:
    auth: AuthService = request.app.state.auth
    try:
        info = auth.authenticate(request.cookies.get(SESSION_COOKIE))
    except AuthError as exc:
        if exc.status_code == 401:
            return {"data": {"authenticated": False}}
        raise HTTPException(exc.status_code, exc.detail) from exc
    return {"data": {"authenticated": True, "expires_at": info.expires_at}}


@router.post("/logout")
def logout(request: Request, response: Response, session: SessionInfo = Depends(require_session)) -> dict:
    request.app.state.auth.logout(session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"data": {"authenticated": False}}
