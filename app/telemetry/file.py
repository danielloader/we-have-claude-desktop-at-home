"""Stub telemetry: one line per transaction, span or metric, appended to a file or stream.

Tail it with `tail -f data/telemetry.log`, or point it at `/dev/stderr` so it lands in the
process output (`docker logs`). Spans and metrics carry the id of the request they happened
in, so a single request can be followed with `grep <id>`.
"""

import contextvars
import json
import sys
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
        # Use the process streams directly rather than reopening the device node, which
        # fails when stderr is a socket (some container runtimes).
        if path in ("-", "stderr", "/dev/stderr"):
            self._fh: IO[str] = sys.stderr
            self._owned = False
        elif path in ("stdout", "/dev/stdout"):
            self._fh = sys.stdout
            self._owned = False
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", buffering=1)  # line-buffered so tail -f sees it immediately
            self._owned = True
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
        if self._owned:
            self._fh.close()
        else:
            self._fh.flush()

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
        self._emit(record)

    def _emit(self, record: dict[str, Any]) -> None:
        self._fh.write((json.dumps(record) if self._fmt == "jsonl" else _text_line(record)) + "\n")
        if not self._owned:
            self._fh.flush()


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
        txn = _Transaction(
            id=uuid.uuid4().hex[:8], name=f"{scope['method']} {scope['path']}", started=time.perf_counter()
        )
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
            self.telemetry._emit(
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
