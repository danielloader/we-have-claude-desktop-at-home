import inspect
from typing import Any, cast

from ..storage.base import ChatStore, UserStore
from .base import Telemetry

_UNTRACED = {"init", "close"}


class _TracedProxy:
    """Wraps every async method of a store in a `store.<method>` span."""

    def __init__(self, inner: Any, telemetry: Telemetry, backend: str) -> None:
        self._inner = inner
        self._telemetry = telemetry
        self._backend = backend

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name in _UNTRACED or not inspect.iscoroutinefunction(attr):
            return attr

        async def traced(*args: Any, **kwargs: Any) -> Any:
            async with self._telemetry.span(f"store.{name}", kind="db", backend=self._backend):
                return await attr(*args, **kwargs)

        return traced


def trace_chat_store(store: ChatStore, telemetry: Telemetry, backend: str) -> ChatStore:
    return cast(ChatStore, _TracedProxy(store, telemetry, backend))


def trace_user_store(store: UserStore, telemetry: Telemetry, backend: str) -> UserStore:
    return cast(UserStore, _TracedProxy(store, telemetry, backend))
