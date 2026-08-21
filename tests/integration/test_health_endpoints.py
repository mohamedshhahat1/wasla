"""Health endpoint integration tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_summary_does_not_probe_dependencies(client, fake_database, fake_redis):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "Wasla"
    assert body["environment"] == "test"
    assert fake_database.calls == 0
    assert fake_redis.calls == 0


async def test_liveness_is_independent_of_dependencies(client, fake_database, fake_redis):
    fake_database.healthy = False
    fake_redis.healthy = False

    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert fake_database.calls == 0


async def test_readiness_reports_every_dependency(client):
    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    components = {component["name"]: component for component in body["components"]}
    assert set(components) == {"postgresql", "redis"}
    assert all(component["status"] == "up" for component in components.values())


async def test_readiness_is_503_when_a_dependency_is_down(client, fake_redis):
    fake_redis.healthy = False

    response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    components = {component["name"]: component for component in body["components"]}
    assert components["postgresql"]["status"] == "up"
    assert components["redis"]["status"] == "down"
    assert components["redis"]["detail"] == "redis is unavailable."


async def test_request_id_header_is_echoed(client):
    response = await client.get("/health/live", headers={"X-Request-ID": "req-test-1"})

    assert response.headers["X-Request-ID"] == "req-test-1"


async def test_request_id_is_generated_when_absent(client):
    response = await client.get("/health/live")

    assert response.headers.get("X-Request-ID")


async def test_unknown_route_returns_the_error_envelope(client):
    response = await client.get("/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["request_id"]


async def test_openapi_schema_exposes_health_routes(client):
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/health/ready" in paths
    assert "/health/live" in paths
