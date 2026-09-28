"""Test setup: real Postgres (the compose db, database ods_delivery_test), tables truncated
before each test, no network except the local MinIO for the file tests."""

import os
import subprocess
import sys
from pathlib import Path

os.environ.update(
    {
        "ENVIRONMENT": "test",
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL",
            "postgresql+asyncpg://ods_delivery:ods_delivery_local@localhost:5451/ods_delivery_test",
        ),
        "DB_NULLPOOL": "false",
        "EMAIL_PROVIDER": "log",
        "PUSH_PROVIDER": "log",
        "SCHEDULER_ENABLED": "false",
        "REALTIME_ENABLED": "true",
        "RATE_LIMIT_ENABLED": "true",
        "CRON_SECRET": "test-cron-secret",
        "GOOGLE_CLIENT_ID": "",
        "GOOGLE_CLIENT_SECRET": "",
        "FIREBASE_CREDENTIALS_PATH": "",
        "LOG_LEVEL": "WARNING",
        "BCRYPT_ROUNDS": "4",
        "S3_BUCKET": "ods-delivery-test",
        # WhatsApp / SMS always OFF unless a test turns them on (HTTP mocked)
        "WHATSAPP_TOKEN": "",
        "WHATSAPP_PHONE_NUMBER_ID": "",
        "WHATSAPP_APP_SECRET": "",
        "WHATSAPP_VERIFY_TOKEN": "",
        "WINSMS_API_KEY": "",
        "WINSMS_SENDER": "",
        "MESSAGING_DISABLED": "false",
        # OSM services: never reached (the transport is replaced below), fast pauses.
        "NOMINATIM_URL": "http://nominatim.test",
        "OVERPASS_URLS": "http://overpass-a.test/api/interpreter,http://overpass-b.test/api/interpreter",
        "NOMINATIM_MIN_INTERVAL_SECONDS": "0",
        "OVERPASS_DELAY_SCALE": "0",
        "OSRM_URL": "",
    }
)
if "ods_delivery_test" not in os.environ["DATABASE_URL"]:
    raise RuntimeError("tests only run against a database named ods_delivery_test")

import httpx
import pytest
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.integrations import osm
from app.main import app as fastapi_app
from app.models import Base
from app.rate_limit import limiter
from app.services import email as email_service
from tests.factories import Factory

ROOT = Path(__file__).resolve().parents[1]
# Messaging fixtures (parties, http, meta_on, sms_on).
pytest_plugins = ["tests.messaging_factories"]


def _alembic(*args: str) -> None:
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, check=True, env=os.environ.copy())


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Every run starts from `downgrade base` + `upgrade head`: both directions stay exercised."""
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")


@pytest.fixture(autouse=True)
async def clean_state() -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    email_service.OUTBOX.clear()
    limiter.reset()


def _refuse_network(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"no network in tests: {request.url.host}", request=request)


@pytest.fixture(autouse=True)
def no_osm_network():
    """Nominatim / Overpass are never called for real: tests install a MockTransport."""
    osm.set_transport(httpx.MockTransport(_refuse_network))
    osm.nominatim_throttle.reset()
    yield
    osm.set_transport(httpx.MockTransport(_refuse_network))


@pytest.fixture
async def session():
    async with SessionLocal() as s:
        yield s


@pytest.fixture
def factory() -> Factory:
    return Factory()


@pytest.fixture
async def client():
    """In-process client without the lifespan (no realtime listener, no scheduler)."""
    transport = httpx.ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver.local") as c:
        yield c
