import json

import httpx
import pytest

from app.llm.base import ChatTurn
from app.llm.ollama import OllamaProvider


def _ndjson(*chunks: dict) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama.test")


@pytest.mark.anyio
async def test_maps_thinking_and_content_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"role": "assistant", "content": "", "thinking": "Let me "}, "done": False},
                {"message": {"role": "assistant", "content": "", "thinking": "think."}, "done": False},
                {"message": {"role": "assistant", "content": "Hello"}, "done": False},
                {"message": {"role": "assistant", "content": " there"}, "done": False},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 21,
                    "eval_count": 9,
                },
            ),
        )

    provider = OllamaProvider(base_url="http://ollama.test", model="qwen3:0.6b", client=_client(handler))
    events = [e async for e in provider.stream([ChatTurn("user", "hi")], system="be brief")]

    assert [e.type for e in events] == [
        "thinking_start",
        "thinking_delta",
        "thinking_delta",
        "thinking_stop",
        "text_delta",
        "text_delta",
        "done",
    ]
    assert "".join(e.text for e in events if e.type == "thinking_delta") == "Let me think."
    assert "".join(e.text for e in events if e.type == "text_delta") == "Hello there"
    assert events[-1].input_tokens == 21 and events[-1].output_tokens == 9
    assert seen["body"]["think"] is True and seen["body"]["model"] == "qwen3:0.6b"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}


@pytest.mark.anyio
async def test_retries_without_think_when_model_lacks_it():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body.get("think"))
        if body.get("think"):
            return httpx.Response(400, json={"error": 'model "smollm2" does not support thinking'})
        return httpx.Response(
            200,
            content=_ndjson(
                {"message": {"role": "assistant", "content": "ok"}, "done": False},
                {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 1},
            ),
        )

    provider = OllamaProvider(base_url="http://ollama.test", model="smollm2", client=_client(handler))
    events = [e async for e in provider.stream([ChatTurn("user", "hi")], system="s")]
    assert calls == [True, None]
    assert [e.type for e in events] == ["text_delta", "done"]
    # Second call skips the failing attempt entirely.
    events = [e async for e in provider.stream([ChatTurn("user", "again")], system="s")]
    assert calls == [True, None, None] and events[-1].type == "done"
