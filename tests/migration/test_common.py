"""migrate/common.py: ids, dates, money, phones, geo, names, categories, masking."""

import uuid
from datetime import UTC, datetime, time
from decimal import Decimal

from migrate import common


def test_det_uuid_is_stable_and_entity_scoped():
    a = common.det_uuid("Order", "abc")
    assert a == common.det_uuid("Order", "abc")
    assert a != common.det_uuid("Message", "abc")
    assert isinstance(a, uuid.UUID) and a.version == 5


def test_b44_id_and_blank():
    assert common.is_b44_id("6978ba65586e4b3336449219")
    assert not common.is_b44_id("PW-123") and not common.is_b44_id(None)
    assert common.blank("  ") and common.blank(None) and not common.blank(0)
    assert common.text_or_none("  x ") == "x" and common.text_or_none("") is None
    assert common.text_or_none("abcdef", 3) == "abc"


def test_parse_dt_formats():
    naive = common.parse_dt("2026-09-28T08:31:58.996000")
    zulu = common.parse_dt("2026-09-28T08:31:58.996Z")
    short = common.parse_dt("2026-09-28T08:31:58")
    assert naive == zulu == datetime(2026, 9, 28, 8, 31, 58, 996000, tzinfo=UTC)
    assert short.tzinfo is UTC
    assert common.parse_dt("not a date") is None and common.parse_dt("") is None
    aware = datetime(2026, 1, 1, tzinfo=UTC)
    assert common.parse_dt(aware) is aware
    assert common.parse_dt(datetime(2026, 1, 1)).tzinfo is UTC


def test_parse_hhmm():
    assert common.parse_hhmm("08:30") == time(8, 30)
    assert common.parse_hhmm("8:05:00") == time(8, 5)
    assert common.parse_hhmm("25:00") is None and common.parse_hhmm("bad") is None
    assert common.parse_hhmm(None) is None


def test_money_and_numbers():
    assert common.money(1087.759712758985) == Decimal("1087.760")
    assert common.money(3) == Decimal("3.000")
    assert common.money("x") is None and common.money(True) is None and common.money("") is None
    assert common.number(4.996, 2) == Decimal("5.00")
    assert common.number("x", 1) is None and common.number(None, 1) is None
    assert common.integer(12.6) == 13 and common.integer("x") is None and common.integer(False) is None


def test_point():
    assert common.point(35.8, 10.6) == "SRID=4326;POINT(10.6 35.8)"
    assert common.point(None, 10) is None
    assert common.point(0, 0) is None
    assert common.point(95, 10) is None
    assert common.point("x", 1) is None and common.point(True, 1) is None


def test_normalize_phone_rules():
    assert common.normalize_phone("22123456") == ("+21622123456", common.PHONE_OK)
    assert common.normalize_phone("+216 22 123 456") == ("+21622123456", common.PHONE_OK)
    assert common.normalize_phone("+216") == (None, common.PHONE_BLANK)
    assert common.normalize_phone("  ") == (None, common.PHONE_BLANK)
    assert common.normalize_phone(None) == (None, common.PHONE_BLANK)
    assert common.normalize_phone("123456789") == (None, common.PHONE_REJECTED)
    # a Swiss mobile in national format is not Tunisian: read in the fallback region only
    assert common.normalize_phone("0791234567") == (None, common.PHONE_REJECTED)
    assert common.normalize_phone("0791234567", ("CH",)) == ("+41791234567", common.PHONE_FALLBACK)
    assert common.normalize_phone("abc", ("CH",)) == (None, common.PHONE_REJECTED)


def test_normalize_name_keeps_arabic():
    assert common.normalize_name("Pharmacie Élise!") == "pharmacie elise"
    assert common.normalize_name("مطعم الأمل") == "مطعم الامل"
    assert common.normalize_name(None) == ""


def test_unify_category():
    assert common.unify_category("pharmacy") == "pharmacie"
    assert common.unify_category("Supermarket") == "supermarché"
    assert common.unify_category("cafe") == "restaurant"
    assert common.unify_category("hôpital") == "hôpital"
    assert common.unify_category("other") == "other"


def test_masks():
    assert common.mask_email("someone@example.test") == "som…@example.test"
    assert common.mask_email("noatsign") == "no…(8)"
    assert common.mask_text("") == "∅"
    assert common.mask_phone("+216 22 123 456") == "11 digits, …56"
    assert common.mask_phone(None) == "∅"
