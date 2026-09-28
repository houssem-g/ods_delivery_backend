"""refreshOsmIndex / osm_refresh job: Overpass mocked (MockTransport), real upserts into places."""

import re
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.integrations import osm
from app.jobs.registry import JOBS
from app.jobs.scheduler import run_job
from app.models import Place
from app.services import osm_refresh
from app.services.osm_refresh import (
    CATEGORY_RULES,
    UnknownCategory,
    bbox_grid,
    build_query,
    element_to_place,
    quality_score,
    targets_for,
)
from tests.catalog_data import add_place
from tests.factories import auth

URL = "/api/functions/refreshOsmIndex"
TS = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)

ELEMENTS = [
    {
        "type": "node", "id": 1, "lat": 36.80, "lon": 10.18,
        "tags": {"amenity": "pharmacy", "name": "Pharmacie Pasteur", "addr:street": "Rue de Rome",
                 "addr:housenumber": "12", "addr:city": "Tunis", "phone": "+216 71 000 000",
                 "opening_hours": "Mo-Sa 08:00-20:00", "name:ar": "صيدلية باستور"},
    },
    {"type": "way", "id": 2, "center": {"lat": 35.82, "lon": 10.60},
     "tags": {"amenity": "pharmacy", "name:ar": "صَيْدَلِيَّة النور", "addr:state": "Sousse"}},
    {"type": "node", "id": 3, "lat": 36.9, "lon": 10.2, "tags": {"amenity": "pharmacy"}},  # unnamed
    {"type": "relation", "id": 4, "tags": {"name": "Sans position"}},
]  # fmt: skip


class FakeOverpass:
    def __init__(self, elements=None, fail_first_endpoint=False, failing_tiles=()):
        self.elements = ELEMENTS if elements is None else elements
        self.fail_first_endpoint = fail_first_endpoint
        self.failing_tiles = set(failing_tiles)
        self.requests: list[httpx.Request] = []
        self.tile_calls: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_first_endpoint and request.url.host == "overpass-a.test":
            return httpx.Response(429)
        query = parse_qs(request.content.decode())["data"][0]
        tile = re.search(r"\((\d[\d.,]+)\);", query).group(1)
        self.tile_calls[tile] = self.tile_calls.get(tile, 0) + 1
        if tile in self.failing_tiles and self.tile_calls[tile] <= 2:  # both endpoints, first pass
            raise httpx.ConnectError("down", request=request)
        # every tile returns the same elements: the import deduplicates them
        return httpx.Response(200, json={"elements": self.elements})


@pytest.fixture
def overpass():
    def install(fake: FakeOverpass) -> FakeOverpass:
        osm.set_transport(httpx.MockTransport(fake))
        return fake

    return install


def test_grid_and_query():
    tiles = bbox_grid((30.2, 7.5, 37.6, 11.6), 3, 2)
    assert len(tiles) == 6 and tiles[0].startswith("30.2,7.5,") and tiles[-1].endswith(",37.6,11.6")
    query = build_query(CATEGORY_RULES[1], tiles[0])
    assert query.startswith("[out:json][timeout:60];") and query.endswith("out center 5000;")
    assert query.count("node[") == 3 and f'way[amenity="pharmacy"]({tiles[0]});' in query


def test_element_to_place_and_quality():
    first = element_to_place(ELEMENTS[0], "pharmacie", TS)
    assert first is not None
    assert first["osm_id"] == "node/1" and first["name"] == "Pharmacie Pasteur"
    assert first["address"] == "12, Rue de Rome, Tunis" and first["city"] == "Tunis"
    assert first["name_norm"] == "pharmacie pasteur"
    assert first["search_norm"] == "pharmacie pasteur 12 rue de rome tunis tunis"  # address + city
    assert first["quality_score"] == 85 and first["phone"] == "+216 71 000 000"
    assert first["location"] == "SRID=4326;POINT(10.18 36.8)"
    arabic = element_to_place(ELEMENTS[1], "pharmacie", TS)
    assert arabic["osm_id"] == "way/2" and arabic["name_norm"] == "صيدلية النور"
    assert arabic["governorate"] == "Sousse" and arabic["address"] is None and arabic["quality_score"] == 55
    assert element_to_place(ELEMENTS[2], "pharmacie", TS) is None
    assert element_to_place(ELEMENTS[3], "pharmacie", TS) is None
    other = element_to_place(
        {"type": "node", "id": 9, "lat": 1, "lon": 2, "tags": {"name:it": "X"}}, "banque", TS
    )
    assert other["name"] == "X" and other["quality_score"] == 50
    assert quality_score({"name": "a", "contact:website": "w", "contact:phone": "p"}) == 70


def test_targets():
    assert [r.key for r in targets_for("all")] == [r.key for r in CATEGORY_RULES]
    assert [r.key for r in targets_for(" banque ")] == ["banque"]
    assert targets_for("", datetime(2026, 9, 27, tzinfo=UTC))[0].key == "restaurant"  # Sunday
    assert targets_for("", datetime(2026, 9, 28, tzinfo=UTC))[0].key == "pharmacie"  # Monday
    assert targets_for("", datetime(2026, 10, 3, tzinfo=UTC))[0].key == "hôpital"  # Saturday
    with pytest.raises(UnknownCategory):
        targets_for("pharmacy")


async def test_auth(client, factory):
    user = await factory.user()
    assert (await client.post(URL, json={"category": "banque"})).status_code == 401
    assert (await client.post(URL, json={"category": "banque"}, headers=auth(user))).json() == {
        "error": "Unauthorized"
    }
    wrong = await client.post(URL, json={"category": "banque"}, headers={"x-cron-token": "nope"})
    assert wrong.status_code == 401
    admin = await factory.user(role="admin")
    unknown = await client.post(URL, json={"category": "cafés"}, headers=auth(admin))
    assert unknown.status_code == 400 and unknown.json()["error"].startswith(
        "Unknown category 'cafés'. Valid:"
    )


async def test_import_upserts_and_is_idempotent(client, factory, overpass):
    admin = await factory.user(role="admin")
    fake = overpass(FakeOverpass(fail_first_endpoint=True))
    first = await client.post(URL, json={"category": "pharmacie"}, headers=auth(admin))
    assert first.status_code == 200
    body = first.json()
    assert body["success"] is True and isinstance(body["duration_ms"], int) and body["backfilled"] == 0
    assert body["results"] == [
        {"category": "pharmacie", "fetched": 24, "parsed": 2, "created": 2, "updated": 0, "errors": 0,
         "failedTiles": 0},
    ]  # fmt: skip
    assert any("parsed 2 named places (2 unnamed skipped)" in line for line in body["log"])
    assert len(fake.requests) == 12  # 6 tiles, first endpoint refused each time
    assert fake.requests[0].headers["user-agent"].startswith("ODS-Delivery")

    fake.elements = [{**ELEMENTS[0], "tags": {**ELEMENTS[0]["tags"], "name": "Pharmacie Pasteur 2"}}]
    second = await client.post(
        URL, params={"category": "pharmacie"}, headers={"x-cron-token": "test-cron-secret"}
    )
    assert second.json()["results"][0] | {} == {
        "category": "pharmacie", "fetched": 6, "parsed": 1, "created": 0, "updated": 1, "errors": 0,
        "failedTiles": 0,
    }  # fmt: skip
    async with SessionLocal() as s:
        rows = {p.osm_id: p for p in (await s.execute(select(Place))).scalars()}
    assert set(rows) == {"node/1", "way/2"}
    assert rows["node/1"].name == "Pharmacie Pasteur 2" and rows["node/1"].name_norm == "pharmacie pasteur 2"
    assert rows["way/2"].category == "pharmacie" and rows["way/2"].source == "osm"


async def test_failed_tiles_are_retried_once(client, factory, overpass):
    admin = await factory.user(role="admin")
    tiles = bbox_grid(osm_refresh.TUNISIA_BBOX, 3, 2)
    fake = overpass(FakeOverpass(failing_tiles={tiles[0], tiles[3]}))
    body = (await client.post(URL, json={"category": "banque"}, headers=auth(admin))).json()
    result = body["results"][0]
    # a tile fails on both endpoints the first time... then succeeds in the retry pass
    assert fake.tile_calls[tiles[0]] == 3 and result["failedTiles"] == 0 and result["fetched"] == 24
    assert any("retrying 2 failed tile(s)" in line for line in body["log"])


async def test_everything_down_reports_failed_tiles(client, factory):
    admin = await factory.user(role="admin")
    body = (await client.post(URL, json={"category": "carburant"}, headers=auth(admin))).json()
    assert body["success"] is True
    assert body["results"][0]["failedTiles"] == 6 and body["results"][0]["fetched"] == 0
    assert any("FAILED" in line for line in body["log"])


async def test_bad_answers_count_as_failures(overpass):
    for answer in (httpx.Response(200, content=b"not json"), httpx.Response(200, json=[1, 2])):
        osm.set_transport(httpx.MockTransport(lambda r, a=answer: a))
        with pytest.raises(osm.OverpassError):
            await osm.overpass_query("[out:json];")


async def test_upsert_error_is_reported_not_raised(client, factory, overpass, monkeypatch):
    admin = await factory.user(role="admin")
    overpass(FakeOverpass())

    async def broken(session, rows):
        raise RuntimeError("db")

    monkeypatch.setattr(osm_refresh, "upsert_places", broken)
    body = (await client.post(URL, json={"category": "pharmacie"}, headers=auth(admin))).json()
    assert body["results"][0]["errors"] == 2 and body["results"][0]["created"] == 0


async def test_job_backfills_imported_rows(overpass, monkeypatch):
    imported = await add_place("Café Bir Lahjar", 36.79, 10.17, address="Rue du Pacha", normalized=False)
    overpass(FakeOverpass(elements=[]))
    monkeypatch.setattr(osm_refresh, "now_utc", lambda: datetime(2026, 10, 2, tzinfo=UTC))  # Friday
    outcome = await run_job(JOBS["osm_refresh"])
    assert outcome["ok"] is True
    assert outcome["result"]["results"][0]["category"] == "carburant"
    assert outcome["result"]["backfilled"] == 1
    async with SessionLocal() as s:
        place = await s.get(Place, imported.id)
    assert place.name_norm == "cafe bir lahjar" and place.search_norm == "cafe bir lahjar rue du pacha"
    assert JOBS["osm_refresh"].enabled is False  # OSM_REFRESH_ENABLED defaults to false
