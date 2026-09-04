import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import TokenClaims
from ..budget import budget_status
from ..deps import ChatStoreDep, CurrentUser, LLMDep, SettingsDep, TelemetryDep, UserStoreDep
from ..llm.base import ChatTurn
from ..models import Conversation, Message, UsageRecord, new_id, now
from ..storage.base import ChatStore

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["chat"])

DEFAULT_TITLE = "New chat"


class CreateConversationRequest(BaseModel):
    title: str = Field(default=DEFAULT_TITLE, max_length=200)


class ConversationDetail(BaseModel):
    conversation: Conversation
    messages: list[Message]


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)


@router.get("", response_model=list[Conversation])
async def list_conversations(claims: CurrentUser, chats: ChatStoreDep) -> list[Conversation]:
    return await chats.list_conversations(claims.sub)


@router.post("", response_model=Conversation, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest, claims: CurrentUser, chats: ChatStoreDep
) -> Conversation:
    return await chats.create_conversation(claims.sub, body.title)


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, claims: CurrentUser, chats: ChatStoreDep) -> ConversationDetail:
    conv = await _owned(chats, conversation_id, claims)
    return ConversationDetail(conversation=conv, messages=await chats.list_messages(conv.id))


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: str, claims: CurrentUser, chats: ChatStoreDep) -> None:
    conv = await _owned(chats, conversation_id, claims)
    await chats.delete_conversation(conv.id)


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    body: SendMessageRequest,
    claims: CurrentUser,
    chats: ChatStoreDep,
    llm: LLMDep,
    settings: SettingsDep,
    users: UserStoreDep,
    telemetry: TelemetryDep,
) -> StreamingResponse:
    """Append the user's message, then stream the assistant reply as server-sent events.

    Events: thinking_start, thinking_delta, thinking_stop, text_delta, done, error.
    """
    conv = await _owned(chats, conversation_id, claims)
    if settings.enforce_token_budget:
        user = await users.get_user(claims.sub)
        if user is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
        budget = await budget_status(user, chats, settings)
        if budget.remaining <= 0:
            telemetry.counter("budget.rejected", user=claims.username)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Token budget of {budget.budget:,} for this {budget.period} is exhausted.",
            )
    history = await chats.list_messages(conv.id)

    user_msg = Message(
        id=new_id(),
        conversation_id=conv.id,
        user_id=claims.sub,
        role="user",
        content=body.content,
        created_at=now(),
    )
    await chats.append_message(user_msg)
    title = _title_from(body.content) if not history and conv.title == DEFAULT_TITLE else None
    await chats.touch_conversation(conv.id, title=title)

    turns = [ChatTurn(m.role, m.content) for m in history] + [ChatTurn("user", body.content)]

    async def events() -> AsyncIterator[bytes]:
        started = time.monotonic()
        text: list[str] = []
        thinking: list[str] = []
        labels = {"provider": llm.name, "model": llm.model}
        yield _sse("user_message", {"message": user_msg.model_dump(mode="json"), "title": title})
        try:
            async with telemetry.span("llm.stream", kind="external", **labels):
                async for ev in llm.stream(turns, system=settings.system_prompt):
                    if ev.type == "text_delta":
                        if not text:
                            telemetry.gauge("llm.time_to_first_token_ms", round((time.monotonic() - started) * 1000, 1), **labels)
                        text.append(ev.text)
                    elif ev.type == "thinking_delta":
                        thinking.append(ev.text)
                    if ev.type == "done":
                        telemetry.counter("llm.requests", **labels)
                        telemetry.counter("llm.input_tokens", ev.input_tokens, **labels)
                        telemetry.counter("llm.output_tokens", ev.output_tokens, **labels)
                        telemetry.gauge("llm.duration_ms", round((time.monotonic() - started) * 1000, 1), **labels)
                        if ev.stop_reason == "refusal" and not text:
                            text.append("The model declined to answer this request.")
                        assistant = Message(
                            id=new_id(),
                            conversation_id=conv.id,
                            user_id=claims.sub,
                            role="assistant",
                            content="".join(text),
                            thinking="".join(thinking) or None,
                            created_at=now(),
                        )
                        await chats.append_message(assistant)
                        await chats.touch_conversation(conv.id)
                        await chats.record_usage(
                            UsageRecord(
                                id=new_id(),
                                user_id=claims.sub,
                                conversation_id=conv.id,
                                message_id=assistant.id,
                                provider=llm.name,
                                model=llm.model,
                                input_tokens=ev.input_tokens,
                                output_tokens=ev.output_tokens,
                                latency_ms=int((time.monotonic() - started) * 1000),
                                created_at=assistant.created_at,
                            )
                        )
                        yield _sse(
                            "done",
                            {
                                "message": assistant.model_dump(mode="json"),
                                "input_tokens": ev.input_tokens,
                                "output_tokens": ev.output_tokens,
                                "stop_reason": ev.stop_reason,
                            },
                        )
                    else:
                        yield _sse(ev.type, {"text": ev.text})
        except Exception:
            log.exception("stream failed for conversation %s", conv.id)
            yield _sse("error", {"detail": "The model provider failed. Please try again."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _owned(chats: ChatStore, conversation_id: str, claims: TokenClaims) -> Conversation:
    conv = await chats.get_conversation(conversation_id)
    # 404 rather than 403 so users cannot probe for other people's conversation ids.
    if conv is None or conv.user_id != claims.sub:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such conversation")
    return conv


def _title_from(content: str) -> str:
    first_line = content.strip().splitlines()[0] if content.strip() else DEFAULT_TITLE
    return first_line[:60] + ("…" if len(first_line) > 60 else "")


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
