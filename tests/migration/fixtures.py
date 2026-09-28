"""A SYNTHETIC Base44 export shaped like the real one (same fields, formats and defects).

Nothing here comes from the production export: names, e-mails, phones and ids are invented.
The defects reproduced are the ones measured in the audit (DB_AUDIT.md §1-3) and in the
2026-09-28 export: duplicate profiles, CourierProfile ids in user fields, deleted accounts,
dangling order ids, placeholder / foreign phones, aberrant fees, histories that miss the last
transition, orders without shops[] or history, synonym notification types, duplicate tokens,
waiting no-response cases on closed orders, English place categories, empty name_norm…
"""

import hashlib
import json
from pathlib import Path
from typing import Any

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 60
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


def bid(kind: str, n: int | str) -> str:
    """A Base44-looking id (24 hex) derived from a label."""
    return hashlib.md5(f"{kind}:{n}".encode()).hexdigest()[:24]


def ts(day: int, hour: int = 10, minute: int = 0, z: bool = False) -> str:
    """Entity dates are naive UTC with microseconds; `*_at` fields and User dates end with Z."""
    base = f"2026-09-{day:02d}T{hour:02d}:{minute:02d}:00.000"
    return base + "Z" if z else base + "000"


def builtins(kind: str, n: Any, day: int = 1, updated: int | None = None) -> dict[str, Any]:
    return {
        "id": bid(kind, n),
        "created_date": ts(day),
        "updated_date": ts(updated or day, 12),
        "created_by_id": bid("User", "service"),
        "is_sample": False,
    }


# --- accounts ---------------------------------------------------------------------------

ADMIN = "admin@example.test"
CUST1 = "cust.one@example.test"
CUST2 = "Cust.Two@Example.test"  # mixed case in User, lower case in the orders
COUR1 = "courier.one@example.test"
COUR2 = "courier.two@example.test"
QA = "qa.dual@example.test"
DISABLED = "gone.user@example.test"
DELETED = "deleted.user@example.test"  # referenced, but no User row


def user(n: str, email: str, role: str = "user", verified: bool = True, **extra: Any) -> dict[str, Any]:
    row = builtins("User", n)
    row.update(
        {
            "created_date": ts(1, z=True),
            "updated_date": ts(2, z=True),
            "email": email,
            "full_name": f"Person {n}",
            "role": role,
            "_app_role": role,
            "is_verified": verified,
            "disabled": None,
            "disabled_reason": None,
            "force_password_reset": False,
            "is_service": False,
            "collaborator_role": None,
            "app_id": bid("App", 1),
        }
    )
    row.update(extra)
    return row


def profile(n: str, email: str, day: int, **extra: Any) -> dict[str, Any]:
    row = builtins("UserProfile", n, day)
    row.update(
        {
            "user_id": email,
            "role": "customer",
            "phone": None,
            "language": "ar",
            "is_active": True,
            "total_orders": 0,
            "no_response_incidents": 0,
            "is_blacklisted": False,
            "last_incident_date": None,
            "default_address": None,
            "governorate": None,
            "city": None,
            "country": None,
            "default_lat": None,
            "default_lng": None,
            "notification_preferences": None,
            "referred_by_courier_id": None,
            "referred_by_code": None,
            "referred_at": None,
            "whatsapp_opt_in": None,
            "whatsapp_opt_in_at": None,
        }
    )
    row.update(extra)
    return row


def courier(n: str, email: str, phone: str, **extra: Any) -> dict[str, Any]:
    row = builtins("CourierProfile", n)
    row.update(
        {
            "user_id": email,
            "full_name": f"Courier {n}",
            "phone": phone,
            "cin_passport": "A123456",
            "photo_url": None,
            "id_photo_uri": None,
            "vehicle_type": "scooter",
            "max_package_size": "petit",
            "price_per_km": 0.5,
            "min_fee": 3,
            "notification_radius_km": 10,
            "is_online": True,
            "current_lat": 35.83,
            "current_lng": 10.6,
            "verification_status": "verified",
            "total_deliveries": 47,
            "total_earnings": 350,
            "average_rating": 4.8,
            "late_cancellations": 0,
            "service_governorate": None,
            "service_city": None,
            "service_country": None,
            "service_start_time": None,
            "service_end_time": None,
            "referral_code": None,
        }
    )
    row.update(extra)
    return row


CP1, CP2, CP_DEAD = bid("CourierProfile", "c1"), bid("CourierProfile", "c2"), bid("CourierProfile", "dead")


def order(n: str, status: str, customer: str = CUST1, **extra: Any) -> dict[str, Any]:
    row = builtins("Order", n, 10, 11)
    row.update(
        {
            "customer_id": customer,
            "customer_name": "Client Name",
            "customer_phone": "+216 22 123 456",
            "items_text": f"items of {n}",
            "quantity": 1,
            "notes": "",
            "alternatives": "",
            "estimated_price": 10,
            "package_size": "petit",
            "shop_name": "Shop A",
            "shop_address": "Rue A",
            "shop_phone": "",
            "shop_governorate": "Sousse",
            "shop_city": "",
            "shop_lat": 35.82,
            "shop_lng": 10.63,
            "shops": [
                {
                    "name": "Shop A",
                    "address": "Rue A",
                    "lat": 35.82,
                    "lng": 10.63,
                    "items": None,
                    "status": "pending",
                    "purchase_amount": None,
                    "receipt_photo_url": None,
                    "completed_at": None,
                }
            ],
            "current_shop_index": 0,
            "delivery_address": "Rue B",
            "delivery_details": "",
            "delivery_governorate": "Sousse",
            "delivery_city": "Sahloul",
            "delivery_lat": 35.83,
            "delivery_lng": 10.59,
            "preferred_time": "asap",
            "scheduled_time": None,
            "status": status,
            "status_history": [],
            "courier_id": None,
            "courier_user_id": None,
            "courier_name": None,
            "courier_phone": None,
            "courier_photo": None,
            "purchase_amount": None,
            "delivery_fee": None,
            "total_amount": None,
            "payment_method": "cash",
            "payment_status": "pending",
            "price_confirmed_by_customer": False,
            "cancelled_by": None,
            "cancellation_reason": None,
            "cancelled_at": None,
            "customer_rating": None,
            "rating_comment": None,
            "distance_km": 3.2,
            "eta_minutes": 25,
            "no_response_reported": False,
            "no_response_case_id": None,
            "no_response_channels": None,
            "resale_order_id": None,
            "preferred_courier_id": None,
            "last_dispatched_at": None,
            "courier_stats_recorded_at": None,
            "courier_live_lat": None,
            "courier_live_lng": None,
            "courier_live_at": None,
        }
    )
    row.update(extra)
    return row


def hist(*items: tuple[str, int], **extra: Any) -> list[dict[str, Any]]:
    return [
        {
            "status": status,
            "timestamp": ts(10, hour, z=True),
            "lat": None,
            "lng": None,
            "cancelled_by": None,
            "reason": None,
            "source": None,
            **extra,
        }
        for status, hour in items
    ]


def offer(n: str, order_n: str, courier_id: str, status: str, fee: Any = 5, **extra: Any) -> dict[str, Any]:
    row = builtins("OrderOffer", n, 10, 10)
    row.update(
        {
            "order_id": bid("Order", order_n),
            "courier_id": courier_id,
            "courier_user_id": COUR1,
            "customer_id": CUST1,
            "courier_name": "Courier",
            "courier_photo": None,
            "courier_rating": 4.99,
            "courier_vehicle": "scooter",
            "proposed_fee": fee,
            "eta_minutes": 20,
            "distance_km": 2.5,
            "message": None,
            "status": status,
            "created_via": None,
        }
    )
    row.update(extra)
    return row


def message(n: str, order_n: str, sender: str, role: str, content: str = "hello", **extra: Any) -> dict:
    row = builtins("Message", n, 10)
    row.update(
        {
            "order_id": bid("Order", order_n),
            "sender_id": sender,
            "recipient_id": None,
            "sender_role": role,
            "content": content,
            "is_template": False,
            "is_read": True,
        }
    )
    row.update(extra)
    return row


def notification(n: str, user_id: str, kind: str, order_n: str | None = "o1", **extra: Any) -> dict:
    row = builtins("Notification", n, 10)
    row.update(
        {
            "user_id": user_id,
            "order_id": bid("Order", order_n) if order_n else None,
            "type": kind,
            "title_ar": "عنوان",
            "title_fr": "Titre",
            "body_ar": "نص",
            "body_fr": "Texte",
            "metadata": {"recipient_role": "customer"},
            "is_read": False,
        }
    )
    row.update(extra)
    return row


def token(n: str, email: str, value: str, seen: int, **extra: Any) -> dict[str, Any]:
    row = builtins("DeviceToken", n, 5)
    row.update(
        {
            "user_id": email,
            "token": value,
            "endpoint_hash": f"h{n}",
            "platform": "android",
            "provider": "fcm",
            "device_model": "Pixel",
            "app_version": "dev",
            "locale": "fr",
            "failure_count": 0,
            "last_error": None,
            "is_active": True,
            "last_seen_at": ts(seen, z=True),
            "role": None,
            "user_agent": None,
        }
    )
    row.update(extra)
    return row


def case(n: str, order_n: str, status: str, resolution: str | None, **extra: Any) -> dict[str, Any]:
    row = builtins("NoResponseCase", n, 10)
    row.update(
        {
            "order_id": bid("Order", order_n),
            "customer_id": CUST1,
            "courier_id": CP1,
            "courier_user_id": COUR1,
            "purchase_amount": 12,
            "started_at": ts(10, 13, z=True),
            "deadline_at": ts(10, 13, 10, z=True),
            "status": status,
            "final_at": None,
            "resolution": resolution,
            "resolved_at": None,
            "incident_counted": False,
            "customer_answered_late": False,
            "push_devices": 1,
            "whatsapp_log_id": None,
            "messaging_status": "whatsapp_skipped_test",
        }
    )
    row.update(extra)
    return row


def deal(n: str, original: str, buyer: str, **extra: Any) -> dict[str, Any]:
    row = builtins("ResaleOrder", n, 10)
    row.update(
        {
            "original_order_id": original,
            "courier_id": CP1,
            "courier_name": "Courier",
            "courier_phone": "0791234567",
            "items_text": "resold items",
            "shop_name": "Shop A",
            "shop_address": None,
            "purchase_amount": 12,
            "discount_percentage": 20,
            "discounted_price": 10,
            "include_delivery": True,
            "delivery_fee": 3,
            "photo_url": "",
            "courier_lat": 35.8,
            "courier_lng": 10.6,
            "delivery_lat": None,
            "delivery_lng": None,
            "status": "sold",
            "expires_at": ts(11, z=True),
            "buyer_id": buyer,
            "buyer_name": "Buyer",
            "buyer_phone": "+216 22 123 456",
            "delivery_address": "Rue C",
        }
    )
    row.update(extra)
    return row


def place(n: str, osm: str, name: str, category: str, **extra: Any) -> dict[str, Any]:
    row = builtins("PlaceIndex", n, 3)
    row.update(
        {
            "osm_id": osm,
            "name": name,
            "name_norm": "",
            "category": category,
            "address": "",
            "city": "",
            "governorate": "",
            "phone": "",
            "opening_hours": "",
            "lat": 35.8,
            "lng": 10.6,
            "source": "osm",
            "source_ts": ts(3, z=True),
            "quality_score": 0.65,
        }
    )
    row.update(extra)
    return row


def build_export() -> dict[str, list[dict[str, Any]]]:
    users = [
        user("admin", ADMIN, role="admin"),
        user("c1", CUST1),
        user("c2", CUST2, verified=False),
        user("k1", COUR1),
        user("k2", COUR2),
        user("qa", QA),
        user("off", DISABLED, disabled=True),
        user("dup", CUST1.upper()),  # same e-mail, other case
    ]
    profiles = [
        profile("admin", ADMIN, 1),
        # two profiles for CUST1: the most recent wins, its empty fields come from the older one
        profile(
            "c1-old",
            CUST1,
            2,
            phone="+216 22 123 456",
            default_lat=35.8,
            default_lng=10.6,
            country="TN",
            city="Sousse",
        ),
        profile(
            "c1-new",
            CUST1,
            3,
            language="fr",
            phone="",
            default_address="Rue B",
            country="Tunisie",
            notification_preferences={"chat_messages": False, "new_orders": True},
            whatsapp_opt_in=True,
            referred_by_courier_id=CP1,
            referred_by_code="REF1",
        ),
        profile("k1", COUR1, 2, phone="+216", role="customer"),
        profile("k2", COUR2, 2, phone="123456789", role="courier", is_active=False),
        profile("qa", QA, 2, phone="22123456"),
        profile("orphan", DELETED, 2),
    ]
    couriers = [
        courier(
            "c1",
            COUR1,
            "0791234567",
            id_photo_uri="private/u/x/id-photo.jpg",
            referral_code="REF1",
            late_cancellations=2,
            service_start_time="08:30",
            service_end_time="bad",
        ),
        courier(
            "c2",
            COUR2,
            "123456789",
            price_per_km=102,
            min_fee=53,
            notification_radius_km=500000000,
            verification_status="pending",
            referral_code="ref1",
            id_photo_uri="private/u/y/missing.jpg",
            photo_url="https://base44.app/public/old.jpg",
            current_lat=None,
            current_lng=None,
        ),
        courier("dead", DELETED, "+216 22 999 999"),
    ]
    k1 = {
        "courier_id": CP1,
        "courier_user_id": COUR1,
        "courier_name": "Courier c1",
        "courier_phone": "0791234567",
    }
    orders = [
        # delivered, history misses the last transition, 2 stops, live position, rating
        order(
            "o1",
            "delivered",
            **k1,
            delivery_fee=7.5,
            purchase_amount=12.345,
            customer_rating=5,
            rating_comment="",
            status_history=hist(("pending", 9), ("accepted", 10), source="placeOrder"),
            courier_live_lat=35.81,
            courier_live_lng=10.61,
            courier_live_at=ts(10, 11, z=True),
            courier_stats_recorded_at=ts(10, 11, 30, z=True),
            shops=[
                {
                    "name": "Shop A",
                    "address": "Rue A",
                    "lat": 35.82,
                    "lng": 10.63,
                    "items": "bread",
                    "status": "purchased",
                    "purchase_amount": 5,
                    "receipt_photo_url": None,
                    "completed_at": ts(10, 10, 30, z=True),
                },
                {
                    "name": "Shop B",
                    "address": None,
                    "lat": 35.84,
                    "lng": 10.64,
                    "items": None,
                    "status": "weird",
                    "purchase_amount": 9999,
                    "receipt_photo_url": "https://x/r.jpg",
                    "completed_at": None,
                },
            ],
        ),
        # cancelled by the courier who then left: e-mail kept, courier_id gone; no history, no date,
        # no shops[] (the stop comes from shop_*)
        order(
            "o2",
            "cancelled",
            customer=CUST2.lower(),
            courier_user_id=COUR1,
            cancelled_by="courier",
            cancellation_reason="vehicle_issue",
            shops=[],
        ),
        # delivered with an aberrant fee
        order(
            "o3",
            "delivered",
            **k1,
            delivery_fee=1087.759712758985,
            status_history=hist(("pending", 9), ("accepted", 10), ("delivered", 11)),
        ),
        # float fee, rejected phone, no customer name, a reported issue, bad cancelled_by
        order(
            "o4",
            "cancelled",
            delivery_fee=3.14159,
            customer_phone="123456789",
            customer_name=None,
            cancelled_by="robot",
            cancelled_at=ts(10, 12, z=True),
            quantity=250,
            reported_issues=[
                {
                    "type": "customer_not_available",
                    "description": "",
                    "photo_url": "https://x/p.jpg",
                    "reported_at": ts(10, 12, z=True),
                    "reported_by": COUR1,
                }
            ],
            status_history=[*hist(("pending", 9), ("bogus", 10)), {"status": "cancelled", "timestamp": None}],
        ),
        # cancelled with a waiting no-response case and a hot deal
        order(
            "o5",
            "cancelled",
            **k1,
            cancelled_at=ts(10, 14, z=True),
            cancelled_by="system",
            no_response_case_id=bid("NoResponseCase", "n1"),
            no_response_channels={"in_app": True, "push_devices": 1, "whatsapp": "skipped_test", "sms": None},
            status_history=hist(
                ("pending", 9),
                ("accepted", 10),
                ("client_no_response", 13),
                ("cancelled", 14),
                cancelled_by=None,
            ),
        ),
        order("o6", "accepted"),  # needs a courier, has none
        order("o7", "cancelled", customer=DELETED),
        order(
            "o8",
            "delivered",
            customer=CUST1,
            **k1,
            resale_order_id=bid("ResaleOrder", "d1"),
            delivery_fee=0,
            status_history=hist(("pending", 15), ("accepted", 15), ("delivered", 16)),
        ),
        order(
            "o9",
            "cancelled",
            resale_order_id=bid("ResaleOrder", "d2"),
            cancelled_at=ts(10, 9, z=True),
            shops=None,
            shop_name="",
        ),
        order(
            "o10",
            "client_no_response",
            customer=QA,
            **k1,
            status_history=hist(("pending", 9), ("accepted", 10), ("client_no_response", 13)),
        ),
        order(
            "o11",
            "pending",
            customer=QA,
            customer_phone="",
            delivery_address="",
            delivery_lat=None,
            delivery_lng=None,
        ),
    ]
    offers = [
        offer("f1", "o1", CP1, "accepted"),
        offer("f2", "gone", CP1, "expired"),
        offer("f3", "o3", CP1, "accepted", fee=1087.759712758985),
        offer("f4", "o4", CP1, "expired", eta_minutes=6545),
        offer("f5", "o11", CP2, "pending", created_date=ts(10, 9)),
        offer("f6", "o11", CP2, "pending", created_date=ts(10, 10)),
        offer("f7", "o8", CP2, "accepted"),
        offer("f8", "o8", CP1, "accepted", courier_rating=None),
        offer("f9", "o1", CP_DEAD, "rejected"),
    ]
    messages = [
        message("m1", "o1", CP1, "courier"),  # CourierProfile id as sender, no recipient
        message("m2", "gone", CUST1, "customer"),
        message("m3", "o1", bid("Nobody", 1), "courier"),
        message("m4", "o1", CUST1, "customer", recipient_id=None, is_read=False),
        message("m5", "o5", COUR1, "courier", content="x" * 1200, recipient_id=CUST1),
        message("m6", "o5", COUR1, "courier", content="   "),
        message("m7", "o2", CUST2.lower(), "customer"),  # courier left: recipient from courier_user_id
    ]
    offer_id = bid("OrderOffer", "f1")
    notifications = [
        notification("n1", CP1, "new_order"),
        notification("n2", CUST1, "courier_on_way"),
        notification("n3", DELETED, "delivered"),
        notification("n4", CUST1, "order_cancelled", order_n="gone"),
        notification(
            "n5",
            CUST1,
            "new_offer",
            metadata={
                "offer_id": offer_id,
                "order_id": bid("Order", "o1"),
                "case_id": bid("X", 9),
                "courier_id": CP1,
                "sender_id": CP1,
            },
        ),
        notification("n6", CUST1, "made_up_type"),
        notification("n7", QA, "message", order_n=None, is_read=True, metadata=None),
        notification("n8", CUST1, "order_accepted", order_id="qa-live-test"),
    ]
    tokens = [
        token("t1", CUST1, "tok-A", 5),
        token("t2", CUST1, "tok-A", 7),
        token("t3", DELETED, "tok-B", 5),
        token("t4", CUST1, "", 5),
        token(
            "t5",
            COUR1,
            "tok-C",
            6,
            platform="ios",
            is_active=False,
            failure_count=5,
            last_error="unregistered",
        ),
    ]
    cases = [
        case("n1", "o5", "waiting", None),
        case("n2", "gone", "resolved", "customer_confirmed"),
        case("n3", "o10", "waiting", "not_a_resolution", started_at=ts(10, 12, z=True), deadline_at=None),
        case("n4", "o10", "waiting", None),
    ]
    deals = [
        deal("d1", bid("Order", "o5"), CUST1),
        deal("d2", "PW-123456789", CUST1),
        deal("d3", bid("Order", "o5"), DELETED, discounted_price=None, discount_percentage=150),
    ]
    shops = [
        {
            **builtins("Shop", "s1", 2),
            "osm_id": "node/1",
            "name": "Resto 1",
            "address": "Rue S",
            "city": None,
            "governorate": None,
            "phone": None,
            "opening_hours": None,
            "description": None,
            "photo_url": None,
            "latitude": 35.8,
            "longitude": 10.6,
            "categories": ["restaurant", "cafe"],
            "menu_items": [
                {"name": "Plat", "price": 12, "description": None, "photo_url": "https://x/menu.jpg"},
                {"name": "", "price": 1},
                {"name": "Boisson", "price": -1, "photo_url": "https://x/missing.jpg"},
            ],
        },
        {
            **builtins("Shop", "s2", 2),
            "osm_id": "custom_2",
            "name": "No coords",
            "latitude": None,
            "longitude": None,
            "categories": [],
            "menu_items": [],
        },
        {
            **builtins("Shop", "s3", 2),
            "osm_id": "custom_3",
            "name": "Proposed",
            "latitude": 35.7,
            "longitude": 10.5,
            "categories": [],
            "menu_items": [],
            "review_status": "pending",
            "proposed_by": CUST1,
            "proposed_at": ts(2, z=True),
            "photo_url": "https://x/shop.jpg",
        },
    ]
    reviews = [
        {
            **builtins("ShopReview", "r1", 4),
            "shop_osm_id": "node/1",
            "user_id": bid("User", "c1"),
            "rating": 5,
            "comment": "bon",
            "photo_urls": ["https://x/r.jpg"],
        },
        {
            **builtins("ShopReview", "r2", 4),
            "shop_osm_id": "way/2",
            "user_id": bid("User", "c1"),
            "rating": 4,
        },
        {
            **builtins("ShopReview", "r3", 4),
            "shop_osm_id": "node/404",
            "user_id": bid("User", "c1"),
            "rating": 4,
        },
        {
            **builtins("ShopReview", "r4", 4),
            "shop_osm_id": "node/1",
            "user_id": bid("User", "c1"),
            "rating": 9,
        },
        # the map keys a shop as `shop:<Shop id>`: rewritten to the new id
        {
            **builtins("ShopReview", "r5", 4),
            "shop_osm_id": f"shop:{bid('Shop', 's1')}",
            "user_id": bid("User", "c2"),
            "rating": 3,
        },
        # same user, same shop through its OSM id, older: dropped
        {
            **builtins("ShopReview", "r6", 3),
            "shop_osm_id": "node/1",
            "user_id": bid("User", "c2"),
            "rating": 2,
        },
        # search lists key places by name and position: kept unresolved, like the app
        {
            **builtins("ShopReview", "r7", 4),
            "shop_osm_id": "place:Chez X@35.8,10.6",
            "user_id": bid("User", "c2"),
            "rating": 4,
        },
    ]
    places = [
        place("p1", "node/1", "مطعم الأمل", "restaurant"),
        place("p2", "way/2", "Pharmacie Élise", "pharmacy", name_norm="pharmacie elise", quality_score=40000),
        place("p3", "way/2", "Duplicate", "bank"),
        place("p4", "node/4", "No coords", "fuel", lat=None),
        place("p5", "node/5", "Grocery", "grocery", phone="+216 73 000 000"),
    ]
    return {
        "User": users,
        "UserProfile": profiles,
        "CourierProfile": couriers,
        "Order": orders,
        "OrderOffer": offers,
        "Message": messages,
        "Notification": notifications,
        "DeviceToken": tokens,
        "ResaleOrder": deals,
        "NoResponseCase": cases,
        "MessageLog": [{**builtins("MessageLog", 1), "channel": "whatsapp"}],
        "AppSettings": [
            {
                **builtins("AppSettings", "main"),
                "key": "main",
                "support_phone": "+21622123456",
                "support_whatsapp": "+21622123456",
                "updated_by": ADMIN,
            }
        ],
        "DeliveryTariffs": [{**builtins("DeliveryTariffs", 1), "name": "base", "price_per_km": 1}],
        "Shop": shops,
        "ShopReview": reviews,
        "PlaceIndex": places,
    }


def write_export(root: Path, export: dict[str, list[dict[str, Any]]] | None = None) -> Path:
    """Writes the export like export.mjs does: one JSON array per entity, files/ with an index."""
    export = export or build_export()
    root.mkdir(parents=True, exist_ok=True)
    for entity, rows in export.items():
        (root / f"{entity}.json").write_text(json.dumps(rows, ensure_ascii=False))
    (root / "_summary.json").write_text(
        json.dumps(
            {
                "exported_at": "2026-09-28T09:00:00.000Z",
                "summary": {e: {"rows": len(r), "unique": len(r)} for e, r in export.items()},
            }
        )
    )
    files = root / "files"
    files.mkdir(exist_ok=True)
    (files / f"courier_id_photo-{CP1}.jpg").write_bytes(JPEG)
    (files / f"menu_photo-{bid('Shop', 's1')}-0.png").write_bytes(PNG)
    (files / "bad.jpg").write_bytes(b"<html>not an image</html>")
    index = [
        {
            "kind": "courier_id_photo",
            "ref": CP1,
            "file": f"courier_id_photo-{CP1}.jpg",
            "status": 200,
            "content_type": "image/jpeg",
            "bytes": len(JPEG),
        },
        {
            "kind": "menu_photo",
            "ref": f"{bid('Shop', 's1')}:0",
            "file": f"menu_photo-{bid('Shop', 's1')}-0.png",
            "status": 200,
            "bytes": len(PNG),
        },
        {
            "kind": "menu_photo",
            "ref": f"{bid('Shop', 's1')}:2",
            "file": "bad.jpg",
            "status": 200,
            "content_type": "image/jpeg",
        },
    ]
    (files / "_index.json").write_text(json.dumps(index))
    return root
