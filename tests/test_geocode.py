"""geocodeAddress: cache → Nominatim (mocked transport, never the real service) → places index."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app.config import settings
from app.db import SessionLocal
from app.integrations import osm
from app.models import GeocodeCache
from app.security.tokens import now_utc
from app.services.geocode import cache_key
from tests.catalog_data import SOUSSE, TUNIS, add_place
from tests.factories import auth, error_of

URL = "/api/functions/geocodeAddress"


class FakeNominatim:
    """Records the requests; answers `results` (or `status`)."""

    def __init__(self, results=None, status: int = 200, raise_error: bool = False):
        self.results = results if results is not None else []
        self.status = status
        self.raise_error = raise_error
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raise_error:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(self.status, json=self.results)


@pytest.fixture
def nominatim():
    def install(fake: FakeNominatim) -> FakeNominatim:
        osm.set_transport(httpx.MockTransport(fake))
        return fake

    return install


HIT = {
    "lat": "36.8529", "lon": "10.1942", "name": "Avenue Mohamed V", "importance": 0.4567,
    "display_name": "Avenue Mohamed V, Tunis, Gouvernorat de Tunis, 1002, Tunisie",
    "address": {"road": "Avenue Mohamed V", "city": "Tunis", "state": "Gouvernorat de Tunis"},
}  # fmt: skip


async def test_guards(client, factory):
    assert (await client.post(URL, json={"address": "x"})).status_code == 401
    user = await factory.user()
    missing = await client.post(URL, json={"city": "Tunis"}, headers=auth(user))
    assert missing.status_code == 400 and error_of(missing) == "Missing address"
    empty = await client.post(URL, json={"address": "!!! ---"}, headers=auth(user))
    assert empty.status_code == 400
    assert empty.json() == {"found": False, "geocode_status": "failed", "error": "Empty query"}


async def test_nominatim_hit_is_cached(client, factory, nominatim):
    user = await factory.user()
    fake = nominatim(FakeNominatim([HIT]))
    body = {"address": "Avenue Mohamed V", "city": "Tunis", "governorate": "Tunis"}
    first = await client.post(URL, json=body, headers=auth(user))
    assert first.status_code == 200
    assert first.json() == {
        "lat": 36.8529, "lng": 10.1942, "found": True, "geocode_status": "resolved",
        "match": {
            "name": "Avenue Mohamed V", "address": HIT["display_name"], "city": "Tunis",
            "governorate": "Gouvernorat de Tunis", "score": 0.46, "source": "nominatim",
        },
    }  # fmt: skip
    request = fake.requests[0]
    assert request.url.host == "nominatim.test" and request.url.path == "/search"
    assert request.url.params["q"] == "Avenue Mohamed V, Tunis, Tunisie"
    assert request.url.params["countrycodes"] == "tn" and request.url.params["format"] == "jsonv2"
    assert request.headers["user-agent"] == settings.OSM_USER_AGENT

    again = await client.post(URL, json=body, headers=auth(user))
    assert again.json()["lat"] == 36.8529 and again.json()["match"]["cached"] is True
    assert len(fake.requests) == 1
    async with SessionLocal() as s:
        row = await s.get(GeocodeCache, cache_key("Avenue Mohamed V", "Tunis", "Tunis"))
        assert row is not None and row.found and row.provider == "nominatim"


async def test_nominatim_miss_is_cached_and_places_answer(client, factory, nominatim):
    user = await factory.user()
    await add_place(
        "Pharmacie Ennasr", 36.86, 10.16, category="pharmacie", address="Rue Hedi Nouira", city="Ariana"
    )
    fake = nominatim(FakeNominatim([]))
    body = {"address": "Pharmacie rue Hedi Nouira", "city": "Ariana"}
    first = await client.post(URL, json=body, headers=auth(user))
    assert first.status_code == 200
    data = first.json()
    assert data["lat"] == pytest.approx(36.86) and data["lng"] == pytest.approx(10.16)
    # 4/5 tokens ("pharmacie", "rue", "hedi", "nouira"; "ariana" matches too → 5/5) + city 0.15 + quality 0.06
    assert data["match"] == {
        "name": "Pharmacie Ennasr", "address": "Rue Hedi Nouira", "city": "Ariana", "governorate": None,
        "score": 1.21, "source": "place_index",
    }  # fmt: skip
    await client.post(URL, json=body, headers=auth(user))
    assert len(fake.requests) == 1  # the miss is remembered: no second Nominatim call
    async with SessionLocal() as s:
        row = await s.get(GeocodeCache, cache_key("Pharmacie rue Hedi Nouira", "Ariana", None))
        assert row is not None and row.found is False


async def test_outage_is_not_cached_and_not_found_is_404(client, factory, nominatim):
    user = await factory.user()
    for fake in (FakeNominatim(raise_error=True), FakeNominatim(status=503), FakeNominatim({"oops": 1})):
        nominatim(fake)
        r = await client.post(URL, json={"address": "Nulle part 123"}, headers=auth(user))
        assert r.status_code == 404
        assert r.json()["found"] is False and r.json()["geocode_status"] == "failed"
        assert len(fake.requests) == 1
    async with SessionLocal() as s:
        assert (await s.execute(select(GeocodeCache))).first() is None


async def test_expired_cache_asks_again(client, factory, nominatim):
    user = await factory.user()
    fake = nominatim(FakeNominatim([HIT]))
    body = {"address": "Avenue Mohamed V"}
    await client.post(URL, json=body, headers=auth(user))
    async with SessionLocal() as s:
        await s.execute(update(GeocodeCache).values(expires_at=now_utc() - timedelta(seconds=1)))
        await s.commit()
    await client.post(URL, json=body, headers=auth(user))
    assert len(fake.requests) == 2


async def test_places_scoring_rules(client, factory, monkeypatch):
    monkeypatch.setattr(settings, "NOMINATIM_ENABLED", False)
    user = await factory.user()
    await add_place("Café Sidi Bou", 36.87, 10.34, city="Sidi Bou Said", quality=100)
    await add_place("Café Central", *SOUSSE, city="Sousse", governorate="Sousse", quality=50)
    await add_place("مخبزة النور", *TUNIS, category="boulangerie", city="Tunis")

    async def geocode(**body):
        return await client.post(URL, json=body, headers=auth(user))

    # "cafe" alone = 1/2 tokens (0.5) + quality: the best one; city boost decides between them
    sousse = await geocode(address="Café inconnu", city="Sousse")
    assert sousse.json()["match"]["name"] == "Café Central"
    # a known, different governorate is excluded; unknown governorates are kept
    assert (await geocode(address="Café Central", governorate="Tunis")).json()["match"][
        "name"
    ] == "Café Sidi Bou"
    weak = await geocode(address="cafe zzz yyy xxx www")  # 1/5 + 0.1 quality < 0.34
    assert weak.status_code == 404
    arabic = await geocode(address="مَخبزة النّور")
    assert arabic.status_code == 200 and arabic.json()["match"]["name"] == "مخبزة النور"


async def test_throttle_falls_back_to_places(client, factory, nominatim, monkeypatch):
    monkeypatch.setattr(settings, "NOMINATIM_MIN_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(settings, "NOMINATIM_MAX_WAIT_SECONDS", 1.0)
    user = await factory.user()
    fake = nominatim(FakeNominatim([HIT]))
    assert (await client.post(URL, json={"address": "Avenue A"}, headers=auth(user))).status_code == 200
    busy = await client.post(URL, json={"address": "Avenue B"}, headers=auth(user))
    assert busy.status_code == 404 and len(fake.requests) == 1  # skipped, not queued for a minute
    async with SessionLocal() as s:
        keys = {row.key for row in (await s.execute(select(GeocodeCache))).scalars()}
    assert keys == {cache_key("Avenue A", None, None)}  # a busy skip is not a cached miss


async def test_throttle_waits_short_gaps(monkeypatch):
    monkeypatch.setattr(settings, "NOMINATIM_MIN_INTERVAL_SECONDS", 0.05)
    osm.nominatim_throttle.reset()
    assert osm.nominatim_throttle.reserve() == 0
    wait = osm.nominatim_throttle.reserve()
    assert wait is not None and 0 < wait <= 0.05
    calls = FakeNominatim([HIT])
    osm.set_transport(httpx.MockTransport(calls))
    assert await osm.nominatim_search("Tunis") is not None  # waited its turn, then asked
    assert len(calls.requests) == 1


async def test_nominatim_skips_malformed_items():
    osm.set_transport(
        httpx.MockTransport(lambda r: httpx.Response(200, json=[1, {"lat": "x", "lon": "1"}, HIT]))
    )
    results = await osm.nominatim_search("Tunis")
    assert [r["lat"] for r in results] == [36.8529]
    osm.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")))
    assert await osm.nominatim_search("Tunis") is None
