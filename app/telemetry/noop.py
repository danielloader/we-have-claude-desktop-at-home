from contextlib import asynccontextmanager

from fastapi import FastAPI


class NoopTelemetry:
    name = "none"

    def install(self, app: FastAPI) -> None:
        pass

    @asynccontextmanager
    async def span(self, name: str, *, kind: str = "app", **labels: str):
        yield

    def counter(self, name: str, value: int = 1, **labels: str) -> None:
        pass

    def gauge(self, name: str, value: float, **labels: str) -> None:
        pass

    def set_user(self, user_id: str, username: str) -> None:
        pass

    async def close(self) -> None:
        pass
