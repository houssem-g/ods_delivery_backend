"""migrate/transform.py on the synthetic export: one test per rule / dirty case (no database)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from migrate import transform as tf
from migrate.common import det_uuid
from tests.migration import fixtures as fx


@pytest.fixture(scope="module")
def export_dir(tmp_path_factory) -> Path:
    return fx.write_export(tmp_path_factory.mktemp("export"))


@pytest.fixture(scope="module")
def bundle(export_dir):
    return tf.transform(export_dir, qa_emails={fx.QA})


def by(rows, key, value):
    return next(r for r in rows if r[key] == value)


def uid(n):
    return det_uuid("User", fx.bid("User", n))


def oid(n):
    return det_uuid("Order", fx.bid("Order", n))


def cid(n):
    return det_uuid("CourierProfile", fx.bid("CourierProfile", n))


def test_ids_are_deterministic(export_dir, bundle):
    again = tf.transform(export_dir, qa_emails={fx.QA})
    assert [r["id"] for r in again.rows("orders")] == [r["id"] for r in bundle.rows("orders")]
    assert by(bundle.rows("orders"), "legacy_b44_id", fx.bid("Order", "o1"))["id"] == oid("o1")


def test_users_merge_profiles_most_recent_wins(bundle):
    users = bundle.rows("users")
    assert len(users) == 7  # 8 exported, one duplicate e-mail (case) excluded
    assert bundle.report.excluded[("User", "duplicate e-mail (case)")] == 1
    cust1 = by(users, "id", uid("c1"))
    assert cust1["legacy_profile_b44_id"] == fx.bid("UserProfile", "c1-new")
    assert cust1["language"] == "fr"  # from the newest profile
    assert cust1["phone_e164"] == "+21622123456"  # newest is empty: taken from the older one
    assert cust1["notify_chat"] is False and cust1["notify_new_orders"] is True
    assert cust1["whatsapp_opt_in_at"] is not None
    assert cust1["profile_created_at"].day == 2  # the first profile's date
    assert bundle.report.notes["duplicate UserProfile merged (most recent kept)"] == 1
    assert bundle.report.excluded[("UserProfile", "no Base44 account with this e-mail")] == 1


def test_user_roles_and_flags(bundle):
    users = {r["id"]: r for r in bundle.rows("users")}
    assert users[uid("admin")]["role"] == "admin"
    assert users[uid("k1")]["role"] == "customer"  # mirrors the profile
    assert users[uid("k2")]["role"] == "courier"
    assert users[uid("k2")]["disabled_at"] is not None  # profile is_active false
    assert users[uid("off")]["disabled_at"] is not None  # User.disabled
    assert users[uid("c2")]["email_verified_at"] is None
    assert users[uid("c2")]["profile_created_at"] is None  # no profile
    assert users[uid("k1")]["phone_e164"] is None  # '+216' alone
    assert users[uid("k2")]["phone_e164"] is None  # placeholder rejected
    assert bundle.report.adjusted[("users", "phone_e164", "phone rejected (not a number) -> NULL")] == 1


def test_default_address_row(bundle):
    address = by(bundle.rows("user_addresses"), "user_id", uid("c1"))
    assert address["is_default"] and address["address"] == "Rue B"
    assert address["location"] == "SRID=4326;POINT(10.6 35.8)"  # from the older profile
    assert address["country"] is None  # 'Tunisie' is not an ISO code
    assert address["city"] == "Sousse"


def test_referral_second_pass(bundle):
    assert bundle.rows("users_referrals") == [{"id": uid("c1"), "referred_by_courier_id": cid("c1")}]


def test_couriers(bundle):
    couriers = {r["legacy_b44_id"]: r for r in bundle.rows("couriers")}
    assert set(couriers) == {fx.CP1, fx.CP2}
    assert bundle.report.excluded[("CourierProfile", "account deleted in Base44 (no User)")] == 1
    c1, c2 = couriers[fx.CP1], couriers[fx.CP2]
    assert c1["phone_e164"] == "+41791234567"  # fallback region
    assert c1["id_document_key"].startswith(f"private/courier_id/{uid('k1')}/")
    assert c1["is_online"] is False and c1["last_location"] is not None
    assert c1["late_cancellations"] == 2 and c1["referral_code"] == "REF1"
    assert str(c1["service_start"]) == "08:30:00" and c1["service_end"] is None
    assert c2["phone_e164"] is None
    assert c2["price_per_km"] == Decimal(50) and c2["notification_radius_km"] == Decimal(100)
    assert c2["min_fee"] == Decimal("53.000")
    assert c2["referral_code"] is None  # same code as c1, case-insensitively
    assert c2["id_document_key"] is None  # file not exported
    assert bundle.report.adjusted[("couriers", "photo_url", "legacy public ID photo URL dropped")] == 1
    audits = [a for a in bundle.rows("audit_log") if a["entity"] == "couriers"]
    assert {next(iter(a["before"])) for a in audits} == {"phone", "price_per_km", "notification_radius_km"}


def test_files(bundle):
    keys = {f.key for f in bundle.files}
    assert len(keys) == 2 == len(bundle.rows("files"))
    private = next(r for r in bundle.rows("files") if r["visibility"] == "private")
    assert private["purpose"] == "courier_id" and private["owner_id"] == uid("k1")
    public = next(r for r in bundle.rows("files") if r["visibility"] == "public")
    assert public["key"].startswith("public/menu/2026/09/") and public["content_type"] == "image/png"
    assert bundle.report.notes["file whose bytes do not match its type skipped (menu)"] == 1


def test_orders_and_exclusions(bundle):
    orders = {r["legacy_b44_id"]: r for r in bundle.rows("orders")}
    assert fx.bid("Order", "o6") not in orders and fx.bid("Order", "o7") not in orders
    assert bundle.report.excluded[("Order", "status accepted without a courier")] == 1
    assert bundle.report.excluded[("Order", "customer account unknown")] == 1
    o1 = orders[fx.bid("Order", "o1")]
    assert o1["courier_id"] == cid("c1") and o1["purchase_amount"] == Decimal("12.345")
    assert o1["delivered_at"].hour == 11  # courier_stats_recorded_at (no delivered event)
    assert o1["accepted_at"].hour == 10
    assert o1["notes"] is None and o1["alternatives"] is None
    o2 = orders[fx.bid("Order", "o2")]
    assert o2["customer_id"] == uid("c2")  # e-mail matched case-insensitively
    assert o2["courier_id"] is None and o2["cancelled_at"] == o2["updated_at"]
    o3 = orders[fx.bid("Order", "o3")]
    assert o3["delivery_fee"] is None and o3["delivered_at"].hour == 11
    o4 = orders[fx.bid("Order", "o4")]
    assert o4["delivery_fee"] == Decimal("3.142")
    assert o4["contact_phone_e164"] is None and o4["contact_name"] == "Person c1"
    assert o4["cancelled_by"] is None and o4["quantity"] == 100
    o11 = orders[fx.bid("Order", "o11")]
    assert o11["delivery_address"] == "-" and o11["delivery_location"] is None
    assert o11["contact_phone_e164"] is None
    fee_audit = [a for a in bundle.rows("audit_log") if a["entity"] == "orders"]
    assert fee_audit[0]["before"] == {"delivery_fee": 1087.76} and fee_audit[0]["after"] == {
        "delivery_fee": None
    }


def test_status_events(bundle):
    events = bundle.rows("order_status_events")

    def of(n):
        return [e for e in events if e["order_id"] == oid(n)]

    o1 = of("o1")
    assert [e["to_status"] for e in o1] == ["pending", "accepted", "delivered"]
    assert o1[-1]["source"] == "migration" and o1[1]["source"] == "placeOrder"
    assert [e["from_status"] for e in o1] == [None, "pending", "accepted"]
    o2 = of("o2")  # no history at all: pending + final synthesized
    assert [e["to_status"] for e in o2] == ["pending", "cancelled"]
    assert o2[-1]["cancelled_by"] == "courier" and o2[-1]["actor_user_id"] == uid("k1")
    assert o2[-1]["reason"] == "vehicle_issue"
    o4 = of("o4")  # an unknown status and an undated item dropped
    assert [e["to_status"] for e in o4] == ["pending", "cancelled"]
    assert (
        bundle.report.adjusted[("order_status_events", "to_status", "history item outside the enum dropped")]
        == 1
    )
    assert bundle.report.notes["status_history last != status (synthetic event added)"] == 2
    for order in bundle.rows("orders"):
        assert of_last(events, order["id"])["to_status"] == order["status"]


def of_last(events, order_id):
    return [e for e in events if e["order_id"] == order_id][-1]


def test_stops(bundle):
    stops = bundle.rows("order_stops")
    o1 = sorted((s for s in stops if s["order_id"] == oid("o1")), key=lambda s: s["seq"])
    assert [s["name"] for s in o1] == ["Shop A", "Shop B"]
    assert o1[0]["status"] == "purchased" and o1[0]["purchase_amount"] == Decimal("5.000")
    assert o1[0]["governorate"] == "Sousse" and o1[1]["governorate"] is None
    assert o1[1]["status"] == "pending" and o1[1]["purchase_amount"] is None
    o2 = [s for s in stops if s["order_id"] == oid("o2")]
    assert len(o2) == 1 and o2[0]["name"] == "Shop A"  # from shop_*
    assert not [s for s in stops if s["order_id"] == oid("o9")]


def test_rating_tracking_issues(bundle):
    assert bundle.rows("order_ratings") == [
        {
            "order_id": oid("o1"),
            "courier_id": cid("c1"),
            "rater_id": uid("c1"),
            "rating": 5,
            "comment": None,
            "created_at": by(bundle.rows("orders"), "id", oid("o1"))["delivered_at"],
        }
    ]
    assert [t["order_id"] for t in bundle.rows("order_tracking")] == [oid("o1")]
    issue = bundle.rows("order_issues")[0]
    assert issue["issue_type"] == "customer_not_available" and issue["reporter_id"] == uid("k1")
    assert issue["photo_key"] is None and issue["description"] is None


def test_offers(bundle):
    offers = {r["legacy_b44_id"]: r for r in bundle.rows("order_offers")}
    assert fx.bid("OrderOffer", "f2") not in offers  # dangling order
    assert fx.bid("OrderOffer", "f3") not in offers  # fee > 200
    assert fx.bid("OrderOffer", "f9") not in offers  # courier excluded
    assert offers[fx.bid("OrderOffer", "f4")]["eta_minutes"] is None
    assert offers[fx.bid("OrderOffer", "f5")]["status"] == "expired"  # older duplicate pending
    assert offers[fx.bid("OrderOffer", "f6")]["status"] == "pending"
    assert offers[fx.bid("OrderOffer", "f7")]["status"] == "rejected"  # not the order's courier
    assert offers[fx.bid("OrderOffer", "f8")]["status"] == "accepted"
    assert offers[fx.bid("OrderOffer", "f8")]["courier_rating_snapshot"] is None
    excluded = [a for a in bundle.rows("audit_log") if a["action"] == tf.AUDIT_EXCLUDE]
    assert excluded[0]["before"]["proposed_fee"] == 1087.76


def test_messages(bundle):
    messages = {r["legacy_b44_id"]: r for r in bundle.rows("messages")}
    m1 = messages[fx.bid("Message", "m1")]
    assert m1["sender_id"] == uid("k1") and m1["recipient_id"] == uid("c1")
    assert messages[fx.bid("Message", "m3")]["sender_id"] is None
    assert messages[fx.bid("Message", "m4")]["recipient_id"] == uid("k1")
    assert messages[fx.bid("Message", "m4")]["read_at"] is None
    assert len(messages[fx.bid("Message", "m5")]["body"]) == 1000
    assert messages[fx.bid("Message", "m7")]["recipient_id"] == uid("k1")  # from courier_user_id
    assert fx.bid("Message", "m2") not in messages and fx.bid("Message", "m6") not in messages


def test_notifications(bundle):
    notes = {r["legacy_b44_id"]: r for r in bundle.rows("notifications")}
    assert notes[fx.bid("Notification", "n1")]["user_id"] == uid("k1")  # CourierProfile id repaired
    assert notes[fx.bid("Notification", "n2")]["type"] == "on_the_way"
    assert notes[fx.bid("Notification", "n7")]["type"] == "new_message"
    assert (
        notes[fx.bid("Notification", "n7")]["data"] == {} and notes[fx.bid("Notification", "n7")]["read_at"]
    )
    assert notes[fx.bid("Notification", "n4")]["order_id"] is None
    assert notes[fx.bid("Notification", "n8")]["order_id"] is None
    data = notes[fx.bid("Notification", "n5")]["data"]
    assert data["offer_id"] == str(det_uuid("OrderOffer", fx.bid("OrderOffer", "f1")))
    assert data["order_id"] == str(oid("o1")) and data["courier_id"] == str(cid("c1"))
    assert data["case_id"] == fx.bid("X", 9) and data["sender_id"] == fx.CP1  # left as is
    assert fx.bid("Notification", "n3") not in notes and fx.bid("Notification", "n6") not in notes


def test_device_tokens(bundle):
    tokens = {r["token"]: r for r in bundle.rows("device_tokens")}
    assert set(tokens) == {"tok-A", "tok-C"}
    assert tokens["tok-A"]["legacy_b44_id"] == fx.bid("DeviceToken", "t2")  # latest seen kept
    assert tokens["tok-C"]["platform"] == "ios" and tokens["tok-C"]["is_active"] is False
    assert bundle.report.excluded_count("DeviceToken") == 3


def test_no_response_cases(bundle):
    cases = {r["legacy_b44_id"]: r for r in bundle.rows("no_response_cases")}
    n1 = cases[fx.bid("NoResponseCase", "n1")]
    assert n1["status"] == "resolved" and n1["resolution"] == "order_cancelled"
    assert n1["channels"] == {"in_app": True, "push_devices": 1, "whatsapp": "skipped_test", "sms": None}
    n3, n4 = cases[fx.bid("NoResponseCase", "n3")], cases[fx.bid("NoResponseCase", "n4")]
    assert n3["resolution"] is None and n3["deadline_at"] == n3["started_at"]
    assert (n3["status"], n4["status"]) == ("expired", "waiting")  # one waiting case per order
    assert fx.bid("NoResponseCase", "n2") not in cases


def test_hot_deals(bundle):
    deals = {r["legacy_b44_id"]: r for r in bundle.rows("hot_deals")}
    d1 = deals[fx.bid("ResaleOrder", "d1")]
    assert d1["buyer_id"] == uid("c1") and d1["buyer_order_id"] == oid("o8") and d1["reserved_at"]
    d3 = deals[fx.bid("ResaleOrder", "d3")]
    assert d3["status"] == "expired" and d3["buyer_id"] is None
    assert d3["price"] == d3["purchase_amount"] and d3["discount_percentage"] == Decimal(100)
    assert fx.bid("ResaleOrder", "d2") not in deals
    assert bundle.rows("orders_resale_links") == [{"id": oid("o8"), "resale_deal_id": d1["id"]}]
    assert bundle.report.adjusted[("orders", "resale_deal_id", "hot deal not migrated -> NULL")] == 1


def test_places_shops_reviews(bundle):
    places = {r["osm_id"]: r for r in bundle.rows("places")}
    assert set(places) == {"node/1", "way/2", "node/5"}
    assert places["node/1"]["name_norm"] == "مطعم الامل"
    assert places["way/2"]["category"] == "pharmacie" and places["way/2"]["quality_score"] == 32767
    assert places["node/5"]["category"] == "supermarché" and places["node/5"]["phone"] == "+216 73 000 000"
    shops = {r["osm_id"]: r for r in bundle.rows("shops")}
    assert set(shops) == {"node/1", "custom_3"}
    assert shops["node/1"]["categories"] == ["restaurant"] and shops["node/1"]["review_status"] == "approved"
    assert shops["node/1"]["_place_osm_id"] == "node/1"
    assert shops["custom_3"]["review_status"] == "pending" and shops["custom_3"]["proposed_by"] == uid("c1")
    items = sorted(bundle.rows("shop_menu_items"), key=lambda r: r["position"])
    assert [i["name"] for i in items] == ["Plat", "Boisson"]
    assert items[0]["photo_key"].startswith("public/menu/") and items[1]["price"] is None
    reviews = {r["legacy_b44_id"]: r for r in bundle.rows("shop_reviews")}
    assert reviews[fx.bid("ShopReview", "r1")]["shop_id"] == shops["node/1"]["id"]
    assert reviews[fx.bid("ShopReview", "r2")]["_place_osm_id"] == "way/2"
    assert bundle.report.excluded_count("ShopReview") == 2


def test_settings_ledger_dropped_and_qa(bundle):
    setting = bundle.rows("app_settings")[0]
    assert setting["key"] == "main" and setting["updated_by"] == uid("admin")
    assert setting["value"] == {"support_phone": "+21622123456", "support_whatsapp": "+21622123456"}
    ledger = bundle.rows("courier_ledger_entries")
    assert [e["order_id"] for e in ledger] == [oid("o1")]  # o3 fee NULLed, o8 fee 0
    assert ledger[0]["kind"] == "commission_waived_launch" and ledger[0]["amount"] == Decimal("0.500")
    assert bundle.report.excluded_count("DeliveryTariffs") == 1
    assert bundle.report.excluded_count("MessageLog") == 1
    assert bundle.report.notes["orders placed by QA accounts"] == 2


def test_every_exported_row_is_kept_or_excluded(bundle):
    one_to_one = (
        "User",
        "CourierProfile",
        "Order",
        "OrderOffer",
        "Message",
        "Notification",
        "ResaleOrder",
        "NoResponseCase",
        "Shop",
        "ShopReview",
        "PlaceIndex",
        "DeviceToken",
        "UserProfile",
    )
    for entity in one_to_one:
        kept = len(bundle.kept.get(entity, set()))
        assert kept + bundle.report.excluded_count(entity) == bundle.report.export_counts[entity], entity


def test_ledger_after_launch_is_not_generated():
    export = fx.build_export()
    for order in export["Order"]:
        order["status_history"] = [h for h in order["status_history"] or [] if h.get("status") != "delivered"]
        order["courier_stats_recorded_at"] = None
        order["updated_date"] = "2027-02-01T10:00:00.000000"
    bundle = tf.Transformer(export).run()
    assert not bundle.rows("courier_ledger_entries")
    assert (
        bundle.report.notes["delivered after the launch end: no ledger entry generated (run statements)"] == 1
    )


def test_duplicate_courier_profiles_and_second_accepted_edge_cases():
    export = fx.build_export()
    extra = fx.courier("c1bis", fx.COUR1, "22123456")
    extra["updated_date"] = "2026-01-01T00:00:00.000000"
    export["CourierProfile"].append(extra)
    export["Order"].append(fx.order("o12", "delivered", courier_id=extra["id"], delivery_fee=2))
    bundle = tf.Transformer(export).run()
    assert (
        bundle.report.excluded[("CourierProfile", "second profile of the same user (references re-pointed)")]
        == 1
    )
    o12 = by(bundle.rows("orders"), "legacy_b44_id", fx.bid("Order", "o12"))
    assert o12["courier_id"] == cid("c1")
    assert o12["delivered_at"] == o12["updated_at"]  # no event, no record date


def test_load_export_errors(tmp_path):
    (tmp_path / "User.json").write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError):
        tf.load_export(tmp_path)
    (tmp_path / "User.json").write_text("[]")
    assert tf.load_export(tmp_path)["Order"] == []
    assert tf.load_file_index(tmp_path) == []


def test_qa_emails_from_constants(tmp_path, monkeypatch):
    path = tmp_path / "constants.ts"
    path.write_text("export const DUAL_EMAIL = process.env.TEST_DUAL_EMAIL ?? 'qa@example.test';\n")
    monkeypatch.setenv("TEST_EXTRA_EMAIL", "other@example.test")
    assert tf.qa_emails_from_constants(path) == {"qa@example.test", "other@example.test"}
    assert tf.qa_emails_from_constants(None) == set()


def test_cli_and_dump(export_dir, tmp_path, capsys):
    out = tmp_path / "intermediate"
    assert tf.main([str(export_dir), "--out", str(out)]) == 0
    assert "orders" in capsys.readouterr().out
    assert (out / "orders.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        tf.dump(tf.transform(export_dir), Path(__file__).parent / "never")
