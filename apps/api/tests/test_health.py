"""Health and readiness endpoints stay green — the smallest possible proof the app boots."""

from httpx import AsyncClient


async def test_root_reports_running(client: AsyncClient) -> None:
    """The base path confirms the service name and that it's running."""
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "running"


async def test_health_is_always_ok(client: AsyncClient) -> None:
    """Liveness never checks dependencies, so it's ok as soon as the process is up."""
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_checks_postgres_and_redis(client: AsyncClient) -> None:
    """Readiness fails closed unless both Postgres and Redis actually answer."""
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["postgres"] == "up"
    assert body["redis"] == "up"
