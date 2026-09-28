async def test_liveness(client):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reports_each_dependency(client):
    response = await client.get("/api/health/ready")
    body = response.json()
    assert body["checks"]["database"] == "ok"
    assert response.status_code == (200 if body["checks"]["storage"] == "ok" else 503)


async def test_client_disconnect_while_reading_the_body_is_not_an_error(caplog):
    """A browser leaving during a function call used to end in a 500 + ERROR traceback."""
    from app.main import app as fastapi_app

    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    path = "/api/functions/getSupportContacts"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234), "server": ("test", 80),
    }  # fmt: skip
    with caplog.at_level("ERROR"):
        await fastapi_app(scope, receive, send)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 499
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
