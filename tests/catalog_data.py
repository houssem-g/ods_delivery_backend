"""Catalog test data: places and shops written straight to the database (real PostGIS rows)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from app.db import SessionLocal
from app.models import Place, Shop, User
from app.services import text_norm

# A few real spots (lat, lng)
TUNIS = (36.8065, 10.1815)
LA_MARSA = (36.8782, 10.3247)
SOUSSE = (35.8256, 10.6084)


def wkt(lat: float, lng: float) -> str:
    return f"SRID=4326;POINT({lng} {lat})"


async def add_place(
    name: str,
    lat: float,
    lng: float,
    category: str = "restaurant",
    *,
    address: str | None = None,
    city: str | None = None,
    governorate: str | None = None,
    quality: int | None = 60,
    osm_id: str | None = None,
    phone: str | None = None,
    normalized: bool = True,
) -> Place:
    async with SessionLocal() as s:
        place = Place(
            osm_id=osm_id or f"node/{uuid.uuid4().int % 10**10}",
            name=name,
            name_norm=text_norm.normalize_text(name) if normalized else "",
            search_norm=text_norm.search_text(name, address, city) if normalized else "",
            category=category,
            address=address,
            city=city,
            governorate=governorate,
            phone=phone,
            location=wkt(lat, lng),
            quality_score=quality,
            source_ts=datetime(2026, 4, 20, tzinfo=UTC),
        )
        s.add(place)
        await s.commit()
        await s.refresh(place)
        return place


async def add_shop(
    name: str,
    lat: float,
    lng: float,
    *,
    review_status: str = "approved",
    proposed_by: User | None = None,
    categories: list[str] | None = None,
    **fields: Any,
) -> Shop:
    async with SessionLocal() as s:
        shop = Shop(
            name=name,
            location=wkt(lat, lng),
            review_status=review_status,
            proposed_by=proposed_by.id if proposed_by else None,
            proposed_at=datetime.now(UTC) if proposed_by else None,
            categories=categories or [],
            **fields,
        )
        s.add(shop)
        await s.commit()
        await s.refresh(shop)
        return shop
