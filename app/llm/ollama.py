"""Ollama provider: a local, CPU-friendly model behind the same LLMProvider protocol.

Talks to Ollama's native streaming API (`POST /api/chat`, one JSON object per line).
Models that support it (qwen3, deepseek-r1, ...) stream a separate `thinking` field, which
maps onto the same thinking events the Anthropic provider emits.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import ChatTurn, StreamEvent

log = logging.getLogger(__name__)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        think: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self._think = think
        # CPU inference is slow: no read timeout, but fail fast if the server is not there.
        self._client = client or httpx.AsyncClient(
            base_url=base_url, timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        )

    async def stream(self, turns: list[ChatTurn], *, system: str) -> AsyncIterator[StreamEvent]:
        messages = [{"role": "system", "content": system}] + [
            {"role": t.role, "content": t.content} for t in turns
        ]
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        if self._think:
            body["think"] = True

        in_thinking = False
        async with self._client.stream("POST", "/api/chat", json=body) as resp:
            if resp.status_code == 400 and self._think:
                detail = (await resp.aread()).decode(errors="replace")
                if "think" in detail:
                    # Model has no thinking mode; remember that and retry once without it.
                    log.info("model %s does not support thinking, disabling", self.model)
                    self._think = False
                    async for ev in self.stream(turns, system=system):
                        yield ev
                    return
            resp.raise_for_status()

            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if err := chunk.get("error"):
                    raise RuntimeError(f"ollama: {err}")
                msg = chunk.get("message") or {}

                if thinking := msg.get("thinking"):
                    if not in_thinking:
                        in_thinking = True
                        yield StreamEvent("thinking_start")
                    yield StreamEvent("thinking_delta", text=thinking)

                if content := msg.get("content"):
                    if in_thinking:
                        in_thinking = False
                        yield StreamEvent("thinking_stop")
                    yield StreamEvent("text_delta", text=content)

                if chunk.get("done"):
                    if in_thinking:
                        yield StreamEvent("thinking_stop")
                    yield StreamEvent(
                        "done",
                        input_tokens=chunk.get("prompt_eval_count", 0),
                        output_tokens=chunk.get("eval_count", 0),
                        stop_reason=chunk.get("done_reason", "stop"),
                    )
