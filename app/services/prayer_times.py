"""Prayer times for Sousse and the « slow » slots of decision D3 (owner, 16/10/2026).

For an order coming from an ODS restaurant (orders.source = 'partner') the courier waits 10 minutes
instead of 3 before « Client ne répond pas » counts, when the report falls:
- around one of the five daily prayers (from 5 min before the prayer time to 25 min after it);
- during Ramadan, from the iftar (Maghrib, 20 min before) to the next Fajr (25 min after it) —
  the iftar table and the Ramadan nights.

No external service: the prayer times are computed astronomically for Sousse (the whole country is
within a few minutes of it, which the windows absorb) with the angles used in Tunisia (Fajr 18°,
Isha 18°, Asr shadow factor 1). The Ramadan dates come from app/services/data/calendar_tn.json
through `forecast.is_ramadan` (2027 estimated; to be completed every year).
Classic Delivery orders are not affected (no new decision for them).
"""

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

SOUSSE = (35.8256, 10.6084)
FAJR_ANGLE = 18.0
ISHA_ANGLE = 18.0
SUNSET_ANGLE = 0.833  # refraction + solar radius
ASR_FACTOR = 1  # Shafii / Maliki (Tunisia)

PRAYERS = ("fajr", "dhuhr", "asr", "maghrib", "isha")
BEFORE = timedelta(minutes=5)
AFTER = timedelta(minutes=25)
IFTAR_BEFORE = timedelta(minutes=20)

NORMAL_WAIT_SECONDS = 180
SLOW_WAIT_SECONDS = 600


def _julian_day(day: date) -> float:
    year, month, d = day.year, day.month, day.day
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + d + b - 1524.5


def _sun(jd: float) -> tuple[float, float]:
    """(declination in degrees, equation of time in hours) for a julian day (praytimes.org)."""
    d = jd - 2451545.0
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    e = math.radians(23.439 - 0.00000036 * d)
    ra = math.degrees(math.atan2(math.cos(e) * math.sin(lam), math.cos(lam))) / 15
    ra %= 24
    decl = math.degrees(math.asin(math.sin(e) * math.sin(lam)))
    eqt = q / 15 - ra
    eqt = (eqt + 12) % 24 - 12
    return decl, eqt


def _hour_angle(angle_deg: float, lat: float, decl: float) -> float:
    """Hours between noon and the sun at `angle_deg` below the horizon (positive)."""
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    cos_h = (-math.sin(math.radians(angle_deg)) - math.sin(lat_r) * math.sin(decl_r)) / (
        math.cos(lat_r) * math.cos(decl_r)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_h)))) / 15


def _asr_angle(factor: int, lat: float, decl: float) -> float:
    """Hours between noon and the Asr (shadow = factor × object + noon shadow)."""
    lat_r, decl_r = math.radians(lat), math.radians(decl)
    altitude = math.atan(1 / (factor + math.tan(abs(lat_r - decl_r))))
    cos_h = (math.sin(altitude) - math.sin(lat_r) * math.sin(decl_r)) / (math.cos(lat_r) * math.cos(decl_r))
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_h)))) / 15


def prayer_times(day: date, where: tuple[float, float] = SOUSSE) -> dict[str, datetime]:
    """The five prayer times of `day` (UTC datetimes) at `where`."""
    lat, lng = where
    jd = _julian_day(day)
    decl, eqt = _sun(jd + 0.5 - lng / 360)  # the sun at local noon
    noon = 12 - lng / 15 - eqt  # UTC hours
    hours = {
        "fajr": noon - _hour_angle(FAJR_ANGLE, lat, decl),
        "dhuhr": noon,
        "asr": noon + _asr_angle(ASR_FACTOR, lat, decl),
        "maghrib": noon + _hour_angle(SUNSET_ANGLE, lat, decl),
        "isha": noon + _hour_angle(ISHA_ANGLE, lat, decl),
    }
    base = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return {name: base + timedelta(hours=value) for name, value in hours.items()}


def _is_ramadan(day: date) -> bool:
    from app.services.forecast import is_ramadan

    return is_ramadan(day)


def slow_slot(now: datetime, where: tuple[float, float] = SOUSSE) -> str | None:
    """The D3 slot `now` (aware datetime) falls into: a prayer's name, 'iftar', 'ramadan_night', or None."""
    now = now.astimezone(UTC)
    today = now.date()
    for day in (today - timedelta(days=1), today, today + timedelta(days=1)):
        times = prayer_times(day, where)
        if _is_ramadan(day):
            iftar = times["maghrib"]
            next_fajr = prayer_times(day + timedelta(days=1), where)["fajr"]
            if iftar - IFTAR_BEFORE <= now <= iftar + AFTER:
                return "iftar"
            if iftar - IFTAR_BEFORE <= now <= next_fajr + AFTER:
                return "ramadan_night"
        for name in PRAYERS:
            if times[name] - BEFORE <= now <= times[name] + AFTER:
                return name
    return None


def wait_seconds(source: str | None, now: datetime) -> int:
    """How long the courier waits at the door before « client ne répond pas » counts: 10 min for a
    restaurant order in a slow slot, else the usual 3 min."""
    if source == "partner" and slow_slot(now) is not None:
        return SLOW_WAIT_SECONDS
    return NORMAL_WAIT_SECONDS


def describe(now: datetime) -> dict[str, Any]:
    """Debug helper: today's times (UTC) and the slot `now` is in."""
    return {
        "times": {k: v.isoformat() for k, v in prayer_times(now.astimezone(UTC).date()).items()},
        "slot": slow_slot(now),
    }
