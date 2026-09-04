import logging
from collections.abc import AsyncIterator
from typing import Any

from anthropic import AsyncAnthropic, AsyncAnthropicFoundry

from .base import ChatTurn, StreamEvent

log = logging.getLogger(__name__)


class AnthropicProvider:
    """Streams Claude via any Anthropic-compatible async client.

    The same class serves the first-party API and Claude on Azure AI (Microsoft Foundry);
    only the client construction and the fallbacks parameter differ.
    """

    def __init__(
        self,
        client: AsyncAnthropic | AsyncAnthropicFoundry,
        *,
        name: str,
        model: str,
        max_tokens: int,
        server_side_fallbacks: bool,
    ) -> None:
        self._client = client
        self.name = name
        self.model = model
        self._max_tokens = max_tokens
        self._fallbacks = server_side_fallbacks

    @classmethod
    def anthropic(cls, *, model: str, max_tokens: int, api_key: str | None = None) -> "AnthropicProvider":
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY or an `ant auth login` profile.
        return cls(
            AsyncAnthropic(api_key=api_key),
            name="anthropic",
            model=model,
            max_tokens=max_tokens,
            server_side_fallbacks=True,
        )

    @classmethod
    def azure(
        cls, *, resource: str, model: str, max_tokens: int, api_key: str | None = None
    ) -> "AnthropicProvider":
        # Server-side refusal fallbacks are a first-party API feature; not available on Foundry.
        return cls(
            AsyncAnthropicFoundry(resource=resource, api_key=api_key),
            name="azure",
            model=model,
            max_tokens=max_tokens,
            server_side_fallbacks=False,
        )

    async def stream(self, turns: list[ChatTurn], *, system: str) -> AsyncIterator[StreamEvent]:
        params: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": t.role, "content": t.content} for t in turns],
            # display="summarized" is what makes the thinking phase visible in the UI;
            # the default on current models streams empty thinking blocks.
            thinking={"type": "adaptive", "display": "summarized"},
        )
        if self._fallbacks:
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"

        block_types: dict[int, str] = {}
        async with self._client.beta.messages.stream(**params) as stream:
            async for event in stream:
                if event.type == "content_block_start":
                    block_types[event.index] = event.content_block.type
                    if event.content_block.type == "thinking":
                        yield StreamEvent("thinking_start")
                elif event.type == "content_block_delta":
                    if event.delta.type == "thinking_delta":
                        yield StreamEvent("thinking_delta", text=event.delta.thinking)
                    elif event.delta.type == "text_delta":
                        yield StreamEvent("text_delta", text=event.delta.text)
                elif event.type == "content_block_stop":
                    if block_types.get(event.index) == "thinking":
                        yield StreamEvent("thinking_stop")

            final = await stream.get_final_message()

        if final.stop_reason == "refusal":
            log.warning(
                "provider=%s model=%s refused: %s",
                self.name,
                final.model,
                getattr(final, "stop_details", None),
            )
        yield StreamEvent(
            "done",
            input_tokens=final.usage.input_tokens,
            output_tokens=final.usage.output_tokens,
            stop_reason=final.stop_reason,
        )
