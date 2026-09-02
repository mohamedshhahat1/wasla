"""The scrape endpoint, over the application it describes.

Three properties, and the third is the one that decides whether any of this is
safe to run in production:

1. It answers in the format a scraper reads, and it can be switched off.
2. It counts requests by *route template*, never by requested path.
3. It cannot break the application it observes.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.metrics import REGISTRY
from app.core.request_metrics import UNMATCHED
from app.main import create_app
from tests.fake_queue_redis import FakeQueueRedis

pytestmark = pytest.mark.integration


class QueueRedis:
    """Mirrors `RedisClient`: the endpoint reaches for `.client`."""

    def __init__(self) -> None:
        self.commands = FakeQueueRedis()
        self.healthy = True
        self.calls = 0

    @property
    def client(self) -> FakeQueueRedis:
        return self.commands

    async def check(self, timeout_seconds: float | None = None) -> None:
        self.calls += 1

    async def close(self) -> None:
        return None


@pytest.fixture
def metrics_app(settings: Settings, fake_database, app: FastAPI) -> FastAPI:
    app.state.redis = QueueRedis()
    return app


@pytest.fixture
async def metrics_client(metrics_app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=metrics_app),
        base_url="http://wasla.test",
    ) as http_client:
        yield http_client


def series(body: str, name: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith(name) and "#" not in line]


# ------------------------------------------------------------ the endpoint


async def test_the_endpoint_answers_in_prometheus_text_format(metrics_client):
    response = await metrics_client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]
    assert "# TYPE wasla_http_requests_total counter" in response.text


async def test_the_queue_and_worker_signals_are_present(metrics_client):
    response = await metrics_client.get("/metrics")

    for name in (
        "wasla_queue_pending_jobs",
        "wasla_queue_inflight_jobs",
        "wasla_queue_delayed_jobs",
        "wasla_queue_dead_letter_jobs",
        "wasla_worker_heartbeat_alive",
    ):
        assert series(response.text, name), f"{name} is missing from the exposition"


async def test_the_endpoint_can_be_switched_off(fake_database, fake_redis):
    """404, not 403: "this deployment serves no metrics" is the true answer.

    403 would also confirm the endpoint exists, which a disabled one should not.
    """
    disabled = create_app(
        Settings(
            _env_file=None,
            environment="test",
            log_format="console",
            log_level="WARNING",
            rate_limit_enabled=False,
            metrics_enabled=False,
        )
    )
    disabled.state.database = fake_database
    disabled.state.redis = fake_redis

    async with AsyncClient(
        transport=ASGITransport(app=disabled), base_url="http://wasla.test"
    ) as client:
        response = await client.get("/metrics")

    assert response.status_code == 404


async def test_the_endpoint_is_absent_from_the_public_schema(metrics_app):
    """A scrape path is deployment shape, not part of the product's API."""
    assert "/metrics" not in metrics_app.openapi()["paths"]


# ------------------------------------------------------------ the labels


async def test_requests_are_counted_under_the_route_template(metrics_client):
    before = REGISTRY.render()
    await metrics_client.get("/health")

    body = (await metrics_client.get("/metrics")).text

    assert 'wasla_http_requests_total{method="GET",route="/health",status="2xx"}' in body
    assert body != before


async def test_a_path_parameter_never_becomes_a_series(metrics_client):
    """The whole point of the route label: one series per route, not per lead."""
    await metrics_client.get("/api/v1/leads/2fb0f0a4-9e6c-4a6a-9d1e-2e4b1f6a7c31")

    body = (await metrics_client.get("/metrics")).text

    assert "2fb0f0a4" not in body


async def test_an_unmatched_path_collapses_into_one_series(metrics_client):
    """A scanner must not be able to name a time series."""
    for path in ("/wp-login.php", "/.env", "/admin/config"):
        await metrics_client.get(path)

    body = (await metrics_client.get("/metrics")).text

    assert f'route="{UNMATCHED}"' in body
    assert "wp-login" not in body
    assert "admin/config" not in body


async def test_an_unknown_method_collapses_into_one_series(metrics_client):
    await metrics_client.request("PROPFIND", "/health")

    body = (await metrics_client.get("/metrics")).text

    assert "PROPFIND" not in body
    assert 'method="OTHER"' in body


async def test_no_metric_label_carries_an_identifier(metrics_client):
    """Scanned over the whole document rather than spot-checked.

    A UUID anywhere in the exposition means something put an identifier into a
    label, which is both a privacy problem and an unbounded-cardinality one.
    """
    await metrics_client.get("/api/v1/leads/2fb0f0a4-9e6c-4a6a-9d1e-2e4b1f6a7c31")
    await metrics_client.get("/health/ready")

    body = (await metrics_client.get("/metrics")).text

    uuids = re.findall(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}", body)
    assert not uuids, f"identifiers reached the exposition: {uuids[:3]}"
    assert "@" not in body


async def test_the_scrape_does_not_count_itself(metrics_client):
    """A scraper counting its own scrapes is a closed loop with a busy route."""
    await metrics_client.get("/metrics")

    body = (await metrics_client.get("/metrics")).text

    assert 'route="/metrics"' not in body
    assert 'route="/health/live"' not in body


# --------------------------------------------------------- it cannot break


async def test_a_dependency_outage_still_serves_what_it_can(metrics_app):
    """Half an exposition beats a 503 during the outage being investigated."""

    class DeadRedis(QueueRedis):
        @property
        def client(self):
            class Broken(FakeQueueRedis):
                async def llen(self, key):
                    raise RuntimeError("Redis is gone")

                async def hgetall(self, key):
                    raise RuntimeError("Redis is gone")

            return Broken()

    metrics_app.state.redis = DeadRedis()
    async with AsyncClient(
        transport=ASGITransport(app=metrics_app), base_url="http://wasla.test"
    ) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "wasla_http_requests_total" in response.text
    assert not series(response.text, "wasla_queue_pending_jobs")


async def test_instrumentation_never_fails_a_business_request(metrics_client, monkeypatch):
    """The invariant: a metric is an observation, never a participant.

    The recorder is broken outright, and the request it was measuring must
    still be served exactly as it would have been.
    """

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the metrics backend is on fire")

    monkeypatch.setattr("app.core.telemetry.HTTP_REQUESTS.increment", explode)
    monkeypatch.setattr("app.core.telemetry.HTTP_LATENCY.observe", explode)

    response = await metrics_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
