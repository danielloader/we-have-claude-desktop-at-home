"""Stub telemetry: one line per transaction, span or metric, appended to a file.

Tail it with `tail -f data/telemetry.log`. Spans and metrics carry the id of the request
they happened in, so a single request can be followed with `grep <id>`.
"""

import contextvars
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal

from fastapi import FastAPI


@dataclass
class _Transaction:
    id: str
    name: str
    started: float
    user: str | None = None
    labels: dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[_Transaction | None] = contextvars.ContextVar("txn", default=None)


class FileTelemetry:
    name = "file"

    def __init__(self, path: str, *, fmt: Literal["text", "jsonl"] = "text") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._fh: IO[str] = open(path, "a", buffering=1)  # line-buffered so tail -f sees it immediately
        self._fmt = fmt

    def install(self, app: FastAPI) -> None:
        app.add_middleware(_RequestMiddleware, telemetry=self)

    @asynccontextmanager
    async def span(self, name: str, *, kind: str = "app", **labels: str):
        started = time.perf_counter()
        error: str | None = None
        try:
            yield
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self._write("span", name, duration_ms=_ms(started), kind=kind, error=error, **labels)

    def counter(self, name: str, value: int = 1, **labels: str) -> None:
        self._write("metric", name, value=value, **labels)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self._write("metric", name, value=value, **labels)

    def set_user(self, user_id: str, username: str) -> None:
        txn = _current.get()
        if txn is not None:
            txn.user = username
            txn.labels["user_id"] = user_id

    async def close(self) -> None:
        self._fh.close()

    # -- internals -------------------------------------------------------------------

    def _write(self, record_type: str, name: str, **fields: Any) -> None:
        txn = _current.get()
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "type": record_type,
            "txn": txn.id if txn else None,
            "name": name,
            **{k: v for k, v in fields.items() if v is not None},
        }
        if self._fmt == "jsonl":
            self._fh.write(json.dumps(record) + "\n")
        else:
            self._fh.write(_text_line(record) + "\n")


def _text_line(r: dict[str, Any]) -> str:
    head = f"{r['ts']} {r['type']:<6} {r['txn'] or '-':<8} {r['name']}"
    tail = " ".join(f"{k}={v}" for k, v in r.items() if k not in ("ts", "type", "txn", "name"))
    return f"{head} {tail}".rstrip()


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class _RequestMiddleware:
    """Pure ASGI so the transaction covers the whole response, streaming included."""

    def __init__(self, app, telemetry: FileTelemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        txn = _Transaction(id=uuid.uuid4().hex[:8], name=f"{scope['method']} {scope['path']}", started=time.perf_counter())
        token = _current.set(txn)
        status = {"code": None}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _current.reset(token)
            route = scope.get("route")
            name = f"{scope['method']} {route.path}" if route is not None else txn.name
            # Written after the response so the line carries status and total duration.
            self.telemetry._fh.write(
                (json.dumps if self.telemetry._fmt == "jsonl" else _text_line)(
                    {
                        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
                        "type": "txn",
                        "txn": txn.id,
                        "name": name,
                        "status": status["code"],
                        "duration_ms": _ms(txn.started),
                        **({"user": txn.user} if txn.user else {}),
                        **txn.labels,
                    }
                )
                + "\n"
            )
