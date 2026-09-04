from datetime import UTC, datetime

from pydantic import BaseModel

from .config import Settings
from .models import User
from .storage.base import ChatStore


class BudgetStatus(BaseModel):
    user_id: str
    username: str
    is_admin: bool
    budget: int
    budget_is_default: bool
    messages: int
    input_tokens: int
    output_tokens: int
    used: int
    remaining: int
    period: str
    period_start: datetime | None


def period_start(period: str, at: datetime | None = None) -> datetime | None:
    at = at or datetime.now(UTC)
    match period:
        case "day":
            return at.replace(hour=0, minute=0, second=0, microsecond=0)
        case "month":
            return at.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        case _:
            return None


async def budget_status(user: User, chats: ChatStore, settings: Settings) -> BudgetStatus:
    since = period_start(settings.budget_period)
    usage = await chats.usage_for_user(user.id, since=since)
    return _status(user, usage.messages, usage.input_tokens, usage.output_tokens, settings, since)


async def budget_statuses(users: list[User], chats: ChatStore, settings: Settings) -> list[BudgetStatus]:
    """One row per user, including users with no usage yet."""
    since = period_start(settings.budget_period)
    by_user = {u.user_id: u for u in await chats.usage_summary(since=since)}
    out = []
    for user in users:
        usage = by_user.get(user.id)
        out.append(
            _status(
                user,
                usage.messages if usage else 0,
                usage.input_tokens if usage else 0,
                usage.output_tokens if usage else 0,
                settings,
                since,
            )
        )
    return out


def _status(user: User, messages: int, inp: int, out: int, settings: Settings, since: datetime | None) -> BudgetStatus:
    budget = user.token_budget if user.token_budget is not None else settings.default_token_budget
    used = inp + out
    return BudgetStatus(
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        budget=budget,
        budget_is_default=user.token_budget is None,
        messages=messages,
        input_tokens=inp,
        output_tokens=out,
        used=used,
        remaining=max(budget - used, 0),
        period=settings.budget_period,
        period_start=since,
    )
