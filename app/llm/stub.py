import asyncio
import random
import re
from collections.abc import AsyncIterator

from .base import ChatTurn, StreamEvent

THINKING_TEMPLATES = [
    'The user is asking about "{prompt}". I should give a direct answer first, then a short '
    "explanation with one concrete example. No need to over-elaborate here.",
    'Let me think about what they actually need. "{prompt}" could be read a couple of ways; '
    "I'll take the most common interpretation and note the alternative briefly.",
    "Breaking this down: what is being asked, what the constraints are, and what a useful "
    'answer looks like. The core of "{prompt}" is fairly standard, so keep it tight.',
]

RESPONSE_TEMPLATE = """Here's a fixed response from the local stub provider.

You asked: "{prompt}"

This is turn {n} of the conversation. Nothing here came from a model; the backend picked a
random initial "thinking" delay and is now streaming this text at a randomised token rate,
with the occasional stall, so the UI behaves the way it will against a real provider.

A few things the stub is good for:

1. Exercising the streaming path end to end without spending tokens.
2. Checking that thinking and text render in the right order.
3. Making sure chat history is persisted and attributed to the logged-in user.

```python
def hello(name: str) -> str:
    return f"hello, {name}"
```

Switch `APP_LLM_PROVIDER` to `anthropic` or `azure` when you want the real thing."""


class StubProvider:
    """Fixed response, streamed with random lead time and randomised token pacing."""

    name = "stub"
    model = "stub-fixed-response"

    def __init__(
        self,
        *,
        lag_range: tuple[float, float] = (0.8, 3.0),
        tokens_per_s: tuple[float, float] = (15.0, 60.0),
        stall_probability: float = 0.03,
        seed: int | None = None,
        response: str = RESPONSE_TEMPLATE,
    ) -> None:
        self._lag_range = lag_range
        self._tokens_per_s = tokens_per_s
        self._stall_probability = stall_probability
        self._rng = random.Random(seed)
        self._response = response

    async def stream(self, turns: list[ChatTurn], *, system: str) -> AsyncIterator[StreamEvent]:
        rng = self._rng
        prompt = next((t.content for t in reversed(turns) if t.role == "user"), "")
        short_prompt = prompt[:60] + ("…" if len(prompt) > 60 else "")

        # Thinking phase: the whole lead time is spent here, with the canned reasoning
        # spread across it so the UI has something to render while it waits.
        lag = rng.uniform(*self._lag_range)
        thinking = rng.choice(THINKING_TEMPLATES).replace("{prompt}", short_prompt)
        thinking_tokens = _tokenise(thinking)
        per_token = lag / max(len(thinking_tokens), 1)
        yield StreamEvent("thinking_start")
        for tok in thinking_tokens:
            await asyncio.sleep(per_token * rng.uniform(0.4, 1.6))
            yield StreamEvent("thinking_delta", text=tok)
        yield StreamEvent("thinking_stop")

        response = self._response.replace("{prompt}", prompt).replace("{n}", str(len(turns)))
        tokens = _tokenise(response)
        # One nominal rate per response; per-token gaps are exponential around it so the
        # cadence looks like a real token stream rather than a metronome.
        rate = rng.uniform(*self._tokens_per_s)
        for tok in tokens:
            delay = rng.expovariate(rate)
            if rng.random() < self._stall_probability:
                delay += rng.uniform(0.3, 1.2)
            await asyncio.sleep(delay)
            yield StreamEvent("text_delta", text=tok)

        yield StreamEvent(
            "done",
            input_tokens=sum(len(_tokenise(t.content)) for t in turns) + len(_tokenise(system)),
            output_tokens=len(tokens) + len(thinking_tokens),
            stop_reason="end_turn",
        )


_TOKEN_RE = re.compile(r"\s*\S{1,5}")


def _tokenise(text: str) -> list[str]:
    """Rough sub-word chunks (leading whitespace attached) so re-joining is lossless."""
    return _TOKEN_RE.findall(text)
