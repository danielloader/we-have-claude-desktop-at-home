from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..auth import create_token, verify_password
from ..budget import BudgetStatus, budget_status
from ..deps import ChatStoreDep, CurrentUser, SettingsDep, UserStoreDep
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: User


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, users: UserStoreDep, settings: SettingsDep) -> LoginResponse:
    user = await users.get_by_username(body.username)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    token = create_token(user, secret=settings.jwt_secret, ttl_minutes=settings.jwt_ttl_minutes)
    return LoginResponse(access_token=token, user=User(**user.model_dump(exclude={"password_hash"})))


@router.get("/me", response_model=User)
async def me(claims: CurrentUser, users: UserStoreDep) -> User:
    user = await users.get_user(claims.sub)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return user


@router.get("/me/usage", response_model=BudgetStatus)
async def my_usage(
    claims: CurrentUser, users: UserStoreDep, chats: ChatStoreDep, settings: SettingsDep
) -> BudgetStatus:
    user = await users.get_user(claims.sub)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return await budget_status(user, chats, settings)
