"""Elastic APM: request transactions via the Starlette middleware, custom spans, and
custom metrics. The agent also auto-instruments httpx, so calls to the Anthropic or
Foundry API show up as outbound spans under the request transaction without extra code.
"""

from contextlib import asynccontextmanager

import elasticapm
from elasticapm.contrib.starlette import ElasticAPM, make_apm_client
from elasticapm.metrics.base_metrics import MetricsSet
from fastapi import FastAPI


class _AppMetrics(MetricsSet):
    """Registered so counters and gauges are collected on the agent's metrics interval."""


class ElasticApmTelemetry:
    name = "elastic_apm"

    def __init__(
        self,
        *,
        server_url: str,
        service_name: str,
        environment: str,
        secret_token: str | None = None,
        api_key: str | None = None,
    ) -> None:
        config = {
            "SERVER_URL": server_url,
            "SERVICE_NAME": service_name,
            "ENVIRONMENT": environment,
            "SECRET_TOKEN": secret_token,
            "API_KEY": api_key,
            "CAPTURE_BODY": "off",
        }
        self._client = make_apm_client({k: v for k, v in config.items() if v is not None})
        self._metrics: MetricsSet = self._client.metrics.register(_AppMetrics)

    def install(self, app: FastAPI) -> None:
        app.add_middleware(ElasticAPM, client=self._client)

    @asynccontextmanager
    async def span(self, name: str, *, kind: str = "app", **labels: str):
        async with elasticapm.async_capture_span(name, span_type=kind, labels=labels or None):
            yield

    def counter(self, name: str, value: int = 1, **labels: str) -> None:
        self._metrics.counter(name, **labels).inc(value)

    def gauge(self, name: str, value: float, **labels: str) -> None:
        self._metrics.gauge(name, **labels).val = value

    def set_user(self, user_id: str, username: str) -> None:
        elasticapm.set_user_context(username=username, user_id=user_id)

    async def close(self) -> None:
        self._client.close()
