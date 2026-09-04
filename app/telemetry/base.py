from contextlib import AbstractAsyncContextManager
from typing import Protocol

from fastapi import FastAPI


class Telemetry(Protocol):
    """Tracing and metrics. One transaction per HTTP request, spans nested inside it."""

    name: str

    def install(self, app: FastAPI) -> None:
        """Add request-tracing middleware. Called once, before the app starts serving."""
        ...

    def span(self, name: str, *, kind: str = "app", **labels: str) -> AbstractAsyncContextManager[None]: ...
    def counter(self, name: str, value: int = 1, **labels: str) -> None: ...
    def gauge(self, name: str, value: float, **labels: str) -> None: ...
    def set_user(self, user_id: str, username: str) -> None:
        """Attach the authenticated user to the current transaction."""
        ...

    async def close(self) -> None: ...
