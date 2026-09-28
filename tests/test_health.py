async def test_liveness(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_each_dependency(client):
    response = await client.get("/api/health/ready")
    body = response.json()
    assert body["checks"]["database"] == "ok"
    assert response.status_code == (200 if body["checks"]["storage"] == "ok" else 503)
