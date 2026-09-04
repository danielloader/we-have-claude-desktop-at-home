from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .auth import TokenClaims, decode_token
from .config import Settings
from .llm.base import LLMProvider
from .storage.base import ChatStore, UserStore
from .telemetry.base import Telemetry

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_user_store(request: Request) -> UserStore:
    return request.app.state.user_store


def get_chat_store(request: Request) -> ChatStore:
    return request.app.state.chat_store


def get_llm(request: Request) -> LLMProvider:
    return request.app.state.llm


def get_telemetry(request: Request) -> Telemetry:
    return request.app.state.telemetry


async def current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
    telemetry: Annotated[Telemetry, Depends(get_telemetry)],
) -> TokenClaims:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = decode_token(creds.credentials, secret=settings.jwt_secret)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")
    telemetry.set_user(claims.sub, claims.username)
    return claims


async def require_admin(claims: Annotated[TokenClaims, Depends(current_user)]) -> TokenClaims:
    if not claims.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return claims


SettingsDep = Annotated[Settings, Depends(get_settings)]
UserStoreDep = Annotated[UserStore, Depends(get_user_store)]
ChatStoreDep = Annotated[ChatStore, Depends(get_chat_store)]
LLMDep = Annotated[LLMProvider, Depends(get_llm)]
TelemetryDep = Annotated[Telemetry, Depends(get_telemetry)]
CurrentUser = Annotated[TokenClaims, Depends(current_user)]
AdminUser = Annotated[TokenClaims, Depends(require_admin)]
