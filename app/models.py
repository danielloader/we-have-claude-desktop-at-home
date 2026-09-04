import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

Role = Literal["user", "assistant"]


def new_id() -> str:
    return uuid.uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


class User(BaseModel):
    id: str
    username: str
    is_admin: bool
    token_budget: int | None = None  # None means the configured default applies
    created_at: datetime


class UserWithSecret(User):
    password_hash: str


class Conversation(BaseModel):
    id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class Message(BaseModel):
    id: str
    conversation_id: str
    user_id: str  # from the JWT `sub` claim, not from the request body
    role: Role
    content: str
    thinking: str | None = None
    created_at: datetime


class UsageRecord(BaseModel):
    id: str
    user_id: str
    conversation_id: str
    message_id: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    created_at: datetime


class UsageSummary(BaseModel):
    user_id: str
    messages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
