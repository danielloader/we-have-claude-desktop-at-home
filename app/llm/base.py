from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class ChatTurn:
    role: Literal["user", "assistant"]
    content: str


EventType = Literal["thinking_start", "thinking_delta", "thinking_stop", "text_delta", "done"]


@dataclass(frozen=True)
class StreamEvent:
    """Provider-neutral stream event. `done` carries usage and the stop reason."""

    type: EventType
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    def stream(self, turns: list[ChatTurn], *, system: str) -> AsyncIterator[StreamEvent]: ...
