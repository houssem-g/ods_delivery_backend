"""Vitrine test data: Vitrines created through the admin functions, catalog bodies, cart orders."""

from typing import Any

from app.models import User
from tests.factories import auth
from tests.order_helpers import SOUSSE_HOME, SOUSSE_SHOP

QA_CUSTOMER = "lemelec346@sixoplus.com"  # settings.QA_ACCOUNTS (default list)
QA_COURIER = "vovine2891@sepole.com"
ALWAYS_OPEN = {
    "tz": "Africa/Tunis",
    "weekly": [{"days": [1, 2, 3, 4, 5, 6, 7], "open": "00:00", "close": "24:00"}],
}


async def fn(client, user: User | None, name: str, payload: dict[str, Any] | None = None):
    headers = auth(user) if user is not None else {}
    return await client.post(f"/api/functions/{name}", json=payload or {}, headers=headers)


async def ok(client, user: User | None, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await fn(client, user, name, payload)
    assert r.status_code == 200, f"{name}: {r.status_code} {r.text}"
    return r.json()


def shop_body(name: str = "Chez Amel", at: tuple[float, float] = SOUSSE_SHOP, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "address": "Av. Habib Bourguiba, Sousse",
        "lat": at[0],
        "lng": at[1],
        "phone": "73123456",
        "category": "cafe",
        "city": "Sousse",
        **extra,
    }


async def manual_vitrine(
    client,
    admin: User,
    *,
    name: str = "Chez Amel",
    products: tuple[tuple[str, float], ...] = (("Café crème", 2.5), ("Croissant", 1.8)),
    active: bool = True,
    hours: dict[str, Any] | None = ALWAYS_OPEN,
) -> tuple[str, list[str]]:
    created = await ok(
        client, admin, "createVitrine", {"shop": shop_body(name), "source": "manual", "hours": hours}
    )
    shop_id = created["vitrine"]["shop_id"]
    ids = []
    for product_name, price in products:
        body = await ok(
            client, admin, "upsertVitrineProduct", {"shop_id": shop_id, "name": product_name, "price": price}
        )
        ids.append(body["product"]["id"])
    if active:
        await ok(client, admin, "setVitrineStatus", {"shop_id": shop_id, "status": "active"})
    return shop_id, ids


def catalog_body(
    products: list[dict[str, Any]] | None = None,
    *,
    test: bool = False,
    version: str = "sha256:v1",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "catalog_version": version,
        "test": test,
        "generated_at": "2026-10-17T08:00:00Z",
        "restaurant": {
            "name": "Coffee and Tea",
            "phone": "73123456",
            "address": "Av. Habib Bourguiba, Sousse",
            "lat": SOUSSE_SHOP[0],
            "lng": SOUSSE_SHOP[1],
            "hours": ALWAYS_OPEN,
        },
        "categories": [{"ods_category_id": 12, "name": "Boissons", "position": 0}],
        "products": products
        if products is not None
        else [
            {"ods_menu_id": 345, "ods_category_id": 12, "name": "Café crème", "price": 2.5, "position": 0},
            {
                "ods_menu_id": 346,
                "ods_category_id": 12,
                "name": "Thé à la menthe",
                "price": 1.5,
                "position": 1,
            },
            {
                "ods_menu_id": 347,
                "ods_category_id": 12,
                "name": "Citronnade",
                "price": 3.2,
                "photo_url": "https://ods.example/c.jpg",
                "available": False,
                "position": 2,
            },
        ],
        **extra,
    }


def cart_order(shop_id: str, lines: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "order": {
            "vitrine": {"shop_id": shop_id, "lines": lines, **extra.pop("vitrine", {})},
            "delivery_address": "Rue de la plage 12",
            "delivery_lat": SOUSSE_HOME[0],
            "delivery_lng": SOUSSE_HOME[1],
            "customer_phone": "22111333",
            **extra,
        }
    }
