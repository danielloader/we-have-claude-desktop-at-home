from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import hash_password
from ..budget import BudgetStatus, budget_statuses
from ..deps import AdminUser, ChatStoreDep, SettingsDep, UserStoreDep
from ..models import Conversation, Message, User

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=4)
    is_admin: bool = False
    token_budget: int | None = Field(default=None, ge=0)


class UpdateUserRequest(BaseModel):
    token_budget: int | None = Field(default=None, ge=0)  # null resets to the default budget


class UsageTotals(BaseModel):
    users: int
    messages: int
    input_tokens: int
    output_tokens: int
    used: int
    budget: int


class UsageReport(BaseModel):
    period: str
    default_budget: int
    totals: UsageTotals
    users: list[BudgetStatus]


class ConversationWithOwner(Conversation):
    username: str | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationWithOwner
    messages: list[Message]


@router.get("/users", response_model=list[User])
async def list_users(_: AdminUser, users: UserStoreDep) -> list[User]:
    return await users.list_users()


@router.post("/users", response_model=User, status_code=status.HTTP_201_CREATED)
async def create_user(body: CreateUserRequest, _: AdminUser, users: UserStoreDep) -> User:
    if await users.get_by_username(body.username):
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
    return await users.create_user(
        body.username, hash_password(body.password), body.is_admin, body.token_budget
    )


@router.patch("/users/{user_id}", response_model=User)
async def update_user(user_id: str, body: UpdateUserRequest, _: AdminUser, users: UserStoreDep) -> User:
    if not await users.set_token_budget(user_id, body.token_budget):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")
    user = await users.get_user(user_id)
    assert user is not None
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, admin: AdminUser, users: UserStoreDep) -> None:
    if user_id == admin.sub:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete yourself")
    if not await users.delete_user(user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such user")


@router.get("/usage", response_model=UsageReport)
async def usage(_: AdminUser, chats: ChatStoreDep, users: UserStoreDep, settings: SettingsDep) -> UsageReport:
    rows = await budget_statuses(await users.list_users(), chats, settings)
    return UsageReport(
        period=settings.budget_period,
        default_budget=settings.default_token_budget,
        totals=UsageTotals(
            users=len(rows),
            messages=sum(r.messages for r in rows),
            input_tokens=sum(r.input_tokens for r in rows),
            output_tokens=sum(r.output_tokens for r in rows),
            used=sum(r.used for r in rows),
            budget=sum(r.budget for r in rows),
        ),
        users=rows,
    )


@router.get("/conversations", response_model=list[ConversationWithOwner])
async def all_conversations(
    _: AdminUser, chats: ChatStoreDep, users: UserStoreDep
) -> list[ConversationWithOwner]:
    names = {u.id: u.username for u in await users.list_users()}
    return [
        ConversationWithOwner(**c.model_dump(), username=names.get(c.user_id))
        for c in await chats.list_all_conversations()
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def read_any_conversation(
    conversation_id: str, _: AdminUser, chats: ChatStoreDep, users: UserStoreDep
) -> ConversationDetail:
    conv = await chats.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
    owner = await users.get_user(conv.user_id)
    return ConversationDetail(
        conversation=ConversationWithOwner(**conv.model_dump(), username=owner.username if owner else None),
        messages=await chats.list_messages(conversation_id),
    )
