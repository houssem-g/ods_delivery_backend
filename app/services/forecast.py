"""Courier earnings forecast (« Mes gains » › « Ce mois-ci »): how much the courier should earn by the
end of the month, as a prudent range, with a method that checks itself against reality and corrects.

Why this shape: at launch there is almost no data. Every quantity therefore starts from a PRUDENT PRIOR
(what the courier / his clients declared, discounted; his own tariffs) and each real delivery moves it
(conjugate Bayesian updates: Gamma–Poisson for rates, Beta–Binomial for shares). Nothing is invented:
without any declaration nor history, the part is left out, and with nothing at all no forecast is shown.

The month = fees already earned this month (real) + a simulation of the remaining days:
  1. His invited clients (users.referred_by_courier_id): for each one, an order rate per day
     ~ Gamma(prior from the courier's estimate, else the client's own answer, else the pooled rate of
     invited clients; updated by the client's real orders since he joined), a probability that the
     client is still active (recency: a client silent for much longer than his usual gap fades out),
     and the share of those orders this courier wins ~ Beta(7, 3) updated with what really happened.
  2. His other deliveries (the open market): per weekday, the chance he works ~ Beta (prior from the
     days he declared, updated with the days he really delivered since he joined), and deliveries per
     working day ~ Gamma (prior from his declared weekly deliveries, discounted, updated with reality).
  3. The fee of each delivery: his real fees (resampled) once he has 5, else around his own tariffs.
  4. Day factors learned across all couriers: weekday, public holiday, Ramadan, rain (Open-Meteo
     forecast for Sousse when available). They start at 1 (no effect) and move only with evidence.
  5. SIMULATIONS (default 2 000) of the remaining days -> 25th / 50th / 75th percentiles, then the ODS
     commission of the month (after the launch: 0.250 DT beyond the 20 free deliveries) is taken off.

Self-correction (job `earnings_forecasts`, every night):
  - a WEEK snapshot (the next 7 days) is stored for every forecastable courier;
  - once a week is over it is compared with what really happened (actual fees and deliveries);
  - from those comparisons: a bias factor per courier (shrunk towards the global one, itself shrunk
    towards 1), the day factors above (actual / expected deliveries per kind of day, shrunk), and the
    width of the range (if reality falls outside the 25–75 band too often, the band widens);
  - when the courier's recent forecasts missed by more than MAX_ERROR on average, the app says the
    forecast is « en apprentissage » and shows only a wide range.
"""

import contextlib
import json
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AppSetting,
    Courier,
    CourierClientEstimate,
    EarningsForecast,
    ForecastFactor,
    Order,
    User,
)
from app.security.deps import CurrentUser
from app.services import order_transitions as ot
from app.services.commission import (
    COMMISSION_PER_DELIVERY_TND,
    FREE_DELIVERIES_PER_MONTH,
    LAUNCH_END_DATE,
    TUNIS,
)
from app.services.orders import OrderRefused, courier_of_user
from app.services.referral import first_name, signup_attribution

METHOD = "bayes-mc-v1"
SIMULATIONS = 2000
HISTORY_DAYS = 120
WEEK_DAYS = 7
DECLARED_DISCOUNT = 0.6  # self-declarations are optimistic and include clients counted elsewhere
CLIENT_PRIOR_SHAPE = 1.0  # a weak prior: one real order weighs as much as the declaration
OPEN_PRIOR_DAYS = 3.0  # the declared deliveries/day weigh like 3 observed working days
WEEKDAY_PRIOR_STRENGTH = 4.0
WIN_PRIOR = (7.0, 3.0)  # his clients' orders come to him first: ~70 % won a priori
FADE_DAYS = 45.0  # a client silent beyond twice his usual gap fades with this scale
FEE_BOOTSTRAP_MIN = 5
FEE_SIGMA = 0.30
FACTOR_PRIOR = 30.0  # expected deliveries of evidence before a day factor moves halfway
BIAS_PRIOR_COURIER = 3.0  # evaluated weeks before a courier's own bias counts halfway
BIAS_PRIOR_GLOBAL = 10.0
COVERAGE_TARGET = 0.5  # reality should fall inside the 25–75 band about half the time
MAX_ERROR = 0.6  # mean relative miss above which the forecast is « en apprentissage »
MIN_EVALUATIONS_FOR_STATUS = 3
UNCHECKED_SPREAD = 1.5  # wider band until 3 of his weeks were compared with reality
WEATHER_KEY = "weather_sousse"
RAIN_MM = 2.0

_CALENDAR_FILE = Path(__file__).resolve().parent / "data" / "calendar_tn.json"  # shipped in the image (app/)
# Fixed public holidays (Tunisia) the forecast knows; religious dates are astronomical estimates
# (app/services/data/calendar_tn.json, sources inside) and are corrected by the learned factors anyway.
_FIXED = {(1, 1), (3, 20), (4, 9), (5, 1), (7, 25), (8, 13), (10, 15), (12, 17)}


def _load_calendar() -> tuple[set[date], tuple[date, date] | None]:
    holidays: set[date] = set()
    ramadan = None
    try:
        data = json.loads(_CALENDAR_FILE.read_text(encoding="utf-8"))
        rel = data.get("religious_2027_estimated", {})
        for value in rel.values():
            for d in value if isinstance(value, list) else [value]:
                with contextlib.suppress(TypeError, ValueError):
                    holidays.add(date.fromisoformat(d))
        start = date.fromisoformat(rel["ramadan_start"])
        fitr = sorted(date.fromisoformat(d) for d in rel["eid_al_fitr"])
        ramadan = (start, fitr[0] - timedelta(days=1))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return holidays, ramadan


_RELIGIOUS, _RAMADAN = _load_calendar()


def is_holiday(day: date) -> bool:
    return (day.month, day.day) in _FIXED or day in _RELIGIOUS


def is_ramadan(day: date) -> bool:
    return _RAMADAN is not None and _RAMADAN[0] <= day <= _RAMADAN[1]


def day_kinds(day: date, rainy: set[date]) -> list[str]:
    kinds = [f"weekday_{day.weekday()}"]
    if is_holiday(day):
        kinds.append("holiday")
    if is_ramadan(day):
        kinds.append("ramadan")
    if day in rainy:
        kinds.append("rain")
    return kinds


def tunis_today(now: datetime | None = None) -> date:
    return (now or ot.now_utc()).astimezone(TUNIS).date()


def month_bounds_days(day: date) -> tuple[date, date]:
    first = day.replace(day=1)
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def _start_of(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=TUNIS)


# --------------------------------------------------------------------------------------------- inputs


@dataclass
class ClientPrior:
    customer_id: uuid.UUID
    name: str
    shape: float  # Gamma posterior of the client's orders per day
    rate: float
    p_alive: float
    orders: int
    source: str  # courier_estimate | client_answer | pooled


@dataclass
class Inputs:
    courier: Courier
    today: date
    clients: list[ClientPrior] = field(default_factory=list)
    win_a: float = WIN_PRIOR[0]
    win_b: float = WIN_PRIOR[1]
    weekday_ab: dict[int, tuple[float, float]] = field(default_factory=dict)
    open_shape: float = 0.0  # Gamma posterior of his non-client deliveries per working day
    open_rate: float = 1.0
    open_known: bool = False
    fees: list[float] = field(default_factory=list)
    fee_median: float = 0.0
    month_fees: float = 0.0
    month_deliveries: int = 0
    factors: dict[str, float] = field(default_factory=dict)
    bias: float = 1.0
    spread: float = 1.0
    rainy: set[date] = field(default_factory=set)
    evaluations: int = 0
    mean_error: float | None = None


async def _factors(session: AsyncSession) -> dict[str, float]:
    rows = (await session.execute(select(ForecastFactor))).scalars()
    return {r.key: float(r.value) for r in rows}


async def _rainy_days(session: AsyncSession) -> set[date]:
    row = await session.get(AppSetting, WEATHER_KEY)
    days: set[date] = set()
    if row is not None and isinstance(row.value, dict):
        for d, mm in (row.value.get("daily") or {}).items():
            with contextlib.suppress(TypeError, ValueError):
                if float(mm) >= RAIN_MM:
                    days.add(date.fromisoformat(d))
    return days


async def _delivered(session: AsyncSession, since: datetime, **where: Any) -> list[tuple]:
    stmt = select(Order.customer_id, Order.courier_id, Order.delivered_at, Order.delivery_fee).where(
        Order.status == "delivered", Order.delivered_at >= since
    )
    for column, value in where.items():
        stmt = stmt.where(
            getattr(Order, column).in_(value)
            if isinstance(value, list | set)
            else getattr(Order, column) == value
        )
    return list((await session.execute(stmt)).all())


async def gather(session: AsyncSession, courier: Courier, now: datetime | None = None) -> Inputs:
    now = now or ot.now_utc()
    today = tunis_today(now)
    inp = Inputs(courier=courier, today=today)
    since = now - timedelta(days=HISTORY_DAYS)
    joined = courier.created_at.astimezone(TUNIS).date() if courier.created_at else today
    month_start, _ = month_bounds_days(today)

    # ---- his own deliveries
    mine = await _delivered(session, since, courier_id=courier.id)
    clients = list(
        (
            await session.execute(
                select(
                    User.id, User.full_name, User.declared_monthly_orders, User.referred_at, User.created_at
                ).where(signup_attribution(courier), User.deleted_at.is_(None))
            )
        ).all()
    )
    client_ids = {c[0] for c in clients}
    inp.fees = [float(f) for _, _, _, f in mine if f is not None and f > 0]
    if inp.fees:
        inp.fee_median = median(inp.fees)
    else:
        km = float(courier.price_per_km or 0)
        inp.fee_median = max(float(courier.min_fee or 0), km * 2.5)  # his own tariffs, ~2.5 km
    for _, _, at, fee in mine:
        if at.astimezone(TUNIS).date() >= month_start:
            inp.month_deliveries += 1
            inp.month_fees += float(fee or 0)

    # ---- his invited clients
    if clients:
        orders = await _delivered(session, since, customer_id=client_ids)
        per_client: dict[uuid.UUID, list[tuple]] = {}
        for row in orders:
            per_client.setdefault(row[0], []).append(row)
        estimates = {
            e.customer_id: e.monthly_orders
            for e in (
                await session.execute(
                    select(CourierClientEstimate).where(CourierClientEstimate.courier_id == courier.id)
                )
            ).scalars()
        }
        # pooled rate: orders per client-day over invited clients with some history (all couriers)
        pooled = await _pooled_client_rate(session, since)
        won = sum(1 for r in orders if r[1] == courier.id)
        inp.win_a, inp.win_b = WIN_PRIOR[0] + won, WIN_PRIOR[1] + (len(orders) - won)
        for cid, name, answer, referred_at, created in clients:
            start = (referred_at or created or now).astimezone(TUNIS).date()
            exposure = max(1, min(HISTORY_DAYS, (today - start).days))
            rows = per_client.get(cid, [])
            if cid in estimates:
                mean, source = estimates[cid] / 30.0 * DECLARED_DISCOUNT, "courier_estimate"
            elif answer is not None:
                mean, source = answer / 30.0 * DECLARED_DISCOUNT, "client_answer"
            elif pooled is not None:
                mean, source = pooled, "pooled"
            elif rows:
                mean, source = len(rows) / exposure, "history"  # his own real orders only
            else:
                continue  # nothing known about this client: left out rather than invented
            mean = max(mean, 1e-4)
            shape = CLIENT_PRIOR_SHAPE + len(rows)
            rate = CLIENT_PRIOR_SHAPE / mean + exposure
            post_mean = shape / rate
            stamps = sorted(r[2] for r in rows)
            gap = (today - stamps[-1].astimezone(TUNIS).date()).days if stamps else exposure
            if len(stamps) >= 2:  # his rhythm WHILE active (frequency), not diluted by the silence (recency)
                usual = max(1.0, (stamps[-1] - stamps[0]).days / (len(stamps) - 1))
            else:
                usual = 1.0 / post_mean
            p_alive = 1.0 if gap <= 2 * usual else math.exp(-(gap - 2 * usual) / FADE_DAYS)
            inp.clients.append(
                ClientPrior(
                    cid, first_name(name), shape, rate, max(0.0, min(1.0, p_alive)), len(rows), source
                )
            )

    # ---- the open market (deliveries for other customers)
    open_rows = [r for r in mine if r[0] not in client_ids]
    worked: dict[date, int] = {}
    for _, _, at, _ in open_rows:
        d = at.astimezone(TUNIS).date()
        worked[d] = worked.get(d, 0) + 1
    observe_from = max(joined, today - timedelta(days=56))
    observed_days = [observe_from + timedelta(days=i) for i in range((today - observe_from).days)]
    declared_days = courier.declared_active_days
    for wd in range(7):
        days = [d for d in observed_days if d.weekday() == wd]
        active = sum(1 for d in days if d in worked)
        if declared_days is not None:
            p0 = min(0.95, max(0.02, declared_days / 7.0))
            a, b = p0 * WEEKDAY_PRIOR_STRENGTH, (1 - p0) * WEEKDAY_PRIOR_STRENGTH
        elif days:
            a, b = 1.0, 1.0
        else:
            continue
        inp.weekday_ab[wd] = (a + active, b + len(days) - active)
    open_active = [n for n in worked.values()]
    if courier.declared_weekly_deliveries is not None and declared_days:
        per_day = courier.declared_weekly_deliveries / declared_days * DECLARED_DISCOUNT
        inp.open_shape = OPEN_PRIOR_DAYS * per_day + sum(open_active)
        inp.open_rate = OPEN_PRIOR_DAYS + len(open_active)
        inp.open_known = True
    elif open_active:
        inp.open_shape = 1.0 + sum(open_active)
        inp.open_rate = 1.0 / max(sum(open_active) / len(open_active), 1e-3) + len(open_active)
        inp.open_known = bool(inp.weekday_ab)

    # ---- learned corrections
    inp.factors = await _factors(session)
    inp.rainy = await _rainy_days(session)
    inp.bias, inp.spread, inp.evaluations, inp.mean_error = await _calibration(
        session, courier.id, inp.factors
    )
    if inp.evaluations < MIN_EVALUATIONS_FOR_STATUS:  # never checked against his reality yet: be humble
        inp.spread = max(inp.spread, UNCHECKED_SPREAD)
    return inp


async def _pooled_client_rate(session: AsyncSession, since: datetime) -> float | None:
    """Orders per day of invited clients (all couriers) once they joined; None under 5 such clients."""
    clients = list(
        (
            await session.execute(
                select(User.id, func.coalesce(User.referred_at, User.created_at)).where(
                    User.referred_by_courier_id.is_not(None), User.deleted_at.is_(None)
                )
            )
        ).all()
    )
    if len(clients) < 5:
        return None
    now = ot.now_utc()
    exposure = sum(max(1, min(HISTORY_DAYS, (now - (at or now)).days)) for _, at in clients)
    count = (
        await session.execute(
            select(func.count()).where(
                Order.status == "delivered",
                Order.delivered_at >= since,
                Order.customer_id.in_([c for c, _ in clients]),
            )
        )
    ).scalar_one()
    return count / exposure if exposure else None


async def _calibration(
    session: AsyncSession, courier_id: uuid.UUID, factors: dict[str, float]
) -> tuple[float, float, int, float | None]:
    rows = list(
        (
            await session.execute(
                select(EarningsForecast)
                .where(
                    EarningsForecast.courier_id == courier_id,
                    EarningsForecast.kind == "week",
                    EarningsForecast.evaluated_at.is_not(None),
                )
                .order_by(EarningsForecast.period_end.desc())
                .limit(12)
            )
        ).scalars()
    )
    global_bias = factors.get("bias_global", 1.0)
    if not rows:
        return global_bias, factors.get("spread_global", 1.0), 0, None
    logs = [math.log((float(r.actual_fees) + 1) / (float(r.p50) + 1)) for r in rows]
    own = sum(logs) / (len(logs) + BIAS_PRIOR_COURIER) + math.log(global_bias) * BIAS_PRIOR_COURIER / (
        len(logs) + BIAS_PRIOR_COURIER
    )
    errors = [
        abs(float(r.actual_fees) - float(r.p50)) / max(float(r.p50), float(r.actual_fees), 1.0) for r in rows
    ]
    return math.exp(own), factors.get("spread_global", 1.0), len(rows), sum(errors) / len(errors)


# ----------------------------------------------------------------------------------------- simulation


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    if lam > 30:
        return max(0, round(rng.gauss(lam, math.sqrt(lam))))
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def _day_factor(inp: Inputs, day: date) -> float:
    f = 1.0
    for kind in day_kinds(day, inp.rainy):
        f *= inp.factors.get(kind, 1.0)
    return f


def simulate(
    inp: Inputs, days: list[date], seed: int | None = None, runs: int = SIMULATIONS
) -> dict[str, Any]:
    """Gross fees and deliveries over `days`: percentiles + the expected split (clients / others)."""
    rng = random.Random(seed)
    factors = [_day_factor(inp, d) for d in days]
    total_factor = sum(factors)
    fees_out: list[float] = []
    count_out: list[int] = []
    client_counts = 0
    open_counts = 0
    for _ in range(runs):
        n_client = 0
        win = rng.betavariate(inp.win_a, inp.win_b)
        for c in inp.clients:
            if rng.random() > c.p_alive:
                continue
            lam = rng.gammavariate(c.shape, 1.0 / c.rate) * total_factor
            orders = _poisson(rng, lam)
            n_client += sum(1 for _ in range(orders) if rng.random() < win)
        n_open = 0
        if inp.open_known:
            per_day = rng.gammavariate(max(inp.open_shape, 1e-3), 1.0 / inp.open_rate)
            for d, f in zip(days, factors, strict=True):
                ab = inp.weekday_ab.get(d.weekday())
                if ab is None or rng.random() > rng.betavariate(*ab):
                    continue
                n_open += _poisson(rng, per_day * f)
        n = n_client + n_open
        if inp.fees and len(inp.fees) >= FEE_BOOTSTRAP_MIN:
            gross = sum(rng.choice(inp.fees) for _ in range(n))
        else:
            gross = sum(inp.fee_median * math.exp(rng.gauss(0, FEE_SIGMA)) for _ in range(n))
        fees_out.append(gross * inp.bias)
        count_out.append(n)
        client_counts += n_client
        open_counts += n_open
    fees_out.sort()
    count_out.sort()

    def pct(values: list[float], q: float) -> float:
        return values[min(len(values) - 1, int(q * (len(values) - 1)))] if values else 0.0

    p50 = pct(fees_out, 0.5)
    p25 = p50 - (p50 - pct(fees_out, 0.25)) * inp.spread
    p75 = p50 + (pct(fees_out, 0.75) - p50) * inp.spread
    return {
        "p25": max(0.0, p25),
        "p50": p50,
        "p75": p75,
        "deliveries_p25": pct([float(c) for c in count_out], 0.25),
        "deliveries_p75": pct([float(c) for c in count_out], 0.75),
        "expected_deliveries": sum(count_out) / runs if runs else 0.0,
        "expected_client_deliveries": client_counts / runs if runs else 0.0,
        "expected_open_deliveries": open_counts / runs if runs else 0.0,
        "daily_expected": {
            d.isoformat(): round(f / total_factor * (sum(count_out) / runs), 4) if total_factor else 0
            for d, f in zip(days, factors, strict=True)
        },
    }


def has_signal(inp: Inputs) -> bool:
    return bool(inp.clients) or inp.open_known


def _commission(inp: Inputs, month_deliveries_total: float, month_first: date) -> float:
    if _start_of(month_first) < LAUNCH_END_DATE:
        return 0.0
    return max(0.0, month_deliveries_total - FREE_DELIVERIES_PER_MONTH) * float(COMMISSION_PER_DELIVERY_TND)


def _r3(x: float) -> str:
    return f"{Decimal(str(round(x, 3))).quantize(Decimal('0.001'))}"


def _tips(inp: Inputs, sim: dict[str, Any], days: list[date]) -> list[dict[str, str]]:
    tips: list[dict[str, str]] = []
    # best weekdays (his own history), when known
    if inp.weekday_ab:
        best = sorted(inp.weekday_ab.items(), key=lambda kv: kv[1][0] / (kv[1][0] + kv[1][1]), reverse=True)[
            :2
        ]
        names_fr = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
        names_ar = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
        if inp.open_known and inp.weekday_ab:
            tips.append(
                {
                    "kind": "days",
                    "fr": "Vos jours les plus actifs : "
                    + " et ".join(names_fr[d] for d, _ in best)
                    + ". Restez en ligne ces jours-là.",
                    "ar": "أكثر أيامك نشاطاً: "
                    + " و".join(names_ar[d] for d, _ in best)
                    + ". ابقَ متصلاً في هذه الأيام.",
                }
            )
    # value of one more invited client over the rest of the month (from his own clients, else none)
    if inp.clients:
        per_client = sum(c.shape / c.rate * c.p_alive for c in inp.clients) / len(inp.clients)
        win = inp.win_a / (inp.win_a + inp.win_b)
        value = per_client * 30 * win * inp.fee_median * 5 * inp.bias
        if value >= 1:
            tips.append(
                {
                    "kind": "invite",
                    "fr": f"5 clients invités de plus : environ +{round(value)} DT par mois.",
                    "ar": f"5 حرفاء إضافيون عبر دعوتك: حوالي +{round(value)} د.ت في الشهر.",
                }
            )
    return tips


def _declared(courier: Courier) -> dict[str, int | None]:
    return {
        "weekly_deliveries": courier.declared_weekly_deliveries,
        "active_days": courier.declared_active_days,
        "regular_clients": courier.declared_regular_clients,
    }


async def month_forecast(
    session: AsyncSession, courier: Courier, now: datetime | None = None, seed: int | None = None
) -> dict[str, Any]:
    now = now or ot.now_utc()
    inp = await gather(session, courier, now)
    first, last = month_bounds_days(inp.today)
    if not has_signal(inp):
        return {
            "success": True,
            "available": False,
            "reason": "not_enough_data",
            "declared": _declared(courier),
            "month": first.isoformat()[:7],
            "earned_so_far": _r3(inp.month_fees),
            "deliveries_so_far": inp.month_deliveries,
        }
    days = [inp.today + timedelta(days=i) for i in range((last - inp.today).days + 1)]
    # today is partly done: half of it is still ahead
    sim = simulate(inp, days, seed=seed)
    base = inp.month_fees
    expected_total = inp.month_deliveries + sim["expected_deliveries"]
    commission = _commission(inp, expected_total, first)
    learning = inp.evaluations >= MIN_EVALUATIONS_FOR_STATUS and (inp.mean_error or 0) > MAX_ERROR
    low, mid, high = (
        base + sim["p25"] - commission,
        base + sim["p50"] - commission,
        base + sim["p75"] - commission,
    )
    if learning:  # not reliable yet for this courier: a wider, honest range
        low, high = base + sim["p25"] * 0.6 - commission, base + sim["p75"] * 1.4 - commission
    return {
        "success": True,
        "available": True,
        "method": METHOD,
        "month": first.isoformat()[:7],
        "days_left": len(days),
        "earned_so_far": _r3(base),
        "deliveries_so_far": inp.month_deliveries,
        "low": _r3(max(base - commission, low)),
        "mid": _r3(max(base - commission, mid)),
        "high": _r3(max(base - commission, high)),
        "expected_deliveries": round(expected_total, 1),
        "from_clients": round(sim["expected_client_deliveries"], 1),
        "from_others": round(sim["expected_open_deliveries"], 1),
        "commission_estimate": _r3(commission),
        "status": "learning"
        if learning
        else ("calibrated" if inp.evaluations >= MIN_EVALUATIONS_FOR_STATUS else "starting"),
        "evaluations": inp.evaluations,
        "clients_counted": len(inp.clients),
        "uses_declarations": inp.courier.declared_weekly_deliveries is not None
        or any(c.source != "pooled" for c in inp.clients),
        "tips": _tips(inp, sim, days),
        "declared": _declared(inp.courier),
    }


# ------------------------------------------------------------------------------------- nightly + learn


async def forecastable_couriers(session: AsyncSession, now: datetime) -> list[Courier]:
    since = now - timedelta(days=HISTORY_DAYS)
    recent = select(Order.courier_id).where(Order.status == "delivered", Order.delivered_at >= since)
    referred = select(User.referred_by_courier_id).where(User.referred_by_courier_id.is_not(None))
    stmt = select(Courier).where(
        Courier.verification == "verified",
        (Courier.declared_weekly_deliveries.is_not(None)) | Courier.id.in_(recent) | Courier.id.in_(referred),
    )
    return list((await session.execute(stmt)).scalars())


async def snapshot_week(
    session: AsyncSession, courier: Courier, now: datetime, seed: int | None = None
) -> bool:
    inp = await gather(session, courier, now)
    if not has_signal(inp):
        return False
    start = inp.today + timedelta(days=1)
    days = [start + timedelta(days=i) for i in range(WEEK_DAYS)]
    sim = simulate(inp, days, seed=seed)
    stmt = (
        insert(EarningsForecast)
        .values(
            courier_id=courier.id,
            kind="week",
            as_of=inp.today,
            period_start=start,
            period_end=days[-1],
            p25=_r3(sim["p25"]),
            p50=_r3(sim["p50"]),
            p75=_r3(sim["p75"]),
            expected_deliveries=_r3(sim["expected_deliveries"]),
            method=METHOD,
            details={
                "daily_expected": sim["daily_expected"],
                "bias": inp.bias,
                "spread": inp.spread,
                "clients": len(inp.clients),
                "open_known": inp.open_known,
            },
        )
        .on_conflict_do_nothing()
    )
    await session.execute(stmt)
    return True


async def evaluate(session: AsyncSession, now: datetime) -> dict[str, int]:
    """Compares every finished week snapshot with reality, then re-learns the shared corrections."""
    today = tunis_today(now)
    due = list(
        (
            await session.execute(
                select(EarningsForecast)
                .where(EarningsForecast.evaluated_at.is_(None), EarningsForecast.period_end < today)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    for snap in due:
        rows = await _delivered(session, _start_of(snap.period_start), courier_id=snap.courier_id)
        end = _start_of(snap.period_end + timedelta(days=1))
        rows = [r for r in rows if r[2] < end]
        snap.actual_fees = Decimal(_r3(sum(float(r[3] or 0) for r in rows)))
        snap.actual_deliveries = len(rows)
        actual_by_day: dict[str, int] = {}
        for r in rows:
            k = r[2].astimezone(TUNIS).date().isoformat()
            actual_by_day[k] = actual_by_day.get(k, 0) + 1
        snap.details = {**(snap.details or {}), "actual_by_day": actual_by_day}
        snap.evaluated_at = now
    await session.flush()
    learned = await relearn(session, now) if due else 0
    return {"evaluated": len(due), "factors": learned}


async def _set_factor(session: AsyncSession, key: str, value: float, weight: float) -> None:
    stmt = insert(ForecastFactor).values(key=key, value=_r4(value), weight=_r3(weight))
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["key"],
            set_={"value": stmt.excluded.value, "weight": stmt.excluded.weight, "updated_at": func.now()},
        )
    )


def _r4(x: float) -> str:
    return f"{Decimal(str(round(x, 4))).quantize(Decimal('0.0001'))}"


async def relearn(session: AsyncSession, now: datetime) -> int:
    """Shared corrections from the last 26 weeks of evaluated snapshots (all couriers)."""
    since = tunis_today(now) - timedelta(days=182)
    snaps = list(
        (
            await session.execute(
                select(EarningsForecast).where(
                    EarningsForecast.kind == "week",
                    EarningsForecast.evaluated_at.is_not(None),
                    EarningsForecast.period_start >= since,
                )
            )
        ).scalars()
    )
    if not snaps:
        return 0
    rainy = await _rainy_days(session)
    current = await _factors(session)
    expected: dict[str, float] = {}
    actual: dict[str, float] = {}
    for s in snaps:
        exp_days = (s.details or {}).get("daily_expected") or {}
        act_days = (s.details or {}).get("actual_by_day") or {}
        for d, e in exp_days.items():
            day = date.fromisoformat(d)
            for kind in day_kinds(day, rainy):
                # expected was computed WITH the factor of the time: undo it to learn the raw effect
                raw = float(e) / max(current.get(kind, 1.0), 1e-3)
                expected[kind] = expected.get(kind, 0.0) + raw
                actual[kind] = actual.get(kind, 0.0) + float(act_days.get(d, 0))
    learned = 0
    for kind, e in expected.items():
        value = (FACTOR_PRIOR + actual.get(kind, 0.0)) / (FACTOR_PRIOR + e)
        await _set_factor(session, kind, min(3.0, max(0.33, value)), e)
        learned += 1
    logs = [math.log((float(s.actual_fees) + 1) / (float(s.p50) + 1)) for s in snaps]
    bias = math.exp(sum(logs) / (len(logs) + BIAS_PRIOR_GLOBAL))
    await _set_factor(session, "bias_global", min(3.0, max(0.33, bias)), len(logs))
    inside = sum(1 for s in snaps if float(s.p25) <= float(s.actual_fees) <= float(s.p75))
    coverage = inside / len(snaps)
    spread = current.get("spread_global", 1.0)
    if len(snaps) >= 8:
        spread = min(
            2.5,
            max(
                0.8,
                spread
                * (
                    1.15
                    if coverage < COVERAGE_TARGET - 0.1
                    else 0.95
                    if coverage > COVERAGE_TARGET + 0.2
                    else 1.0
                ),
            ),
        )
    await _set_factor(session, "spread_global", spread, len(snaps))
    await _set_factor(session, "coverage_global", coverage, len(snaps))
    return learned + 3


async def nightly(session: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    now = now or ot.now_utc()
    result = await evaluate(session, now)
    made = 0
    for courier in await forecastable_couriers(session, now):
        made += await snapshot_week(session, courier, now)
    return {**result, "snapshots": made}


# ------------------------------------------------------------------------------------------- functions


async def _courier(session: AsyncSession, user: CurrentUser) -> Courier:
    courier = await courier_of_user(session, user.id)
    if courier is None:
        raise OrderRefused(403, "courier_profile_missing")
    return courier


async def my_forecast(session: AsyncSession, user: CurrentUser) -> dict[str, Any]:
    """getMyEarningsForecast."""
    courier = await _courier(session, user)
    return await month_forecast(session, courier)


def _int_in(value: Any, low: int, high: int, error: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise OrderRefused(400, error)
    try:
        number = int(str(value).strip())
    except ValueError:
        raise OrderRefused(400, error) from None
    if number < low or number > high:
        raise OrderRefused(400, error, min=low, max=high)
    return number


async def save_activity(session: AsyncSession, user: CurrentUser, payload: dict[str, Any]) -> dict[str, Any]:
    """saveMyCourierActivity: what the courier already does outside the app (all optional)."""
    courier = await courier_of_user(session, user.id, lock=True)
    if courier is None:
        raise OrderRefused(403, "courier_profile_missing")
    mapping = {
        "weekly_deliveries": ("declared_weekly_deliveries", 300),
        "active_days": ("declared_active_days", 7),
        "regular_clients": ("declared_regular_clients", 500),
    }
    for key, (attr, high) in mapping.items():
        if key in payload:
            setattr(courier, attr, _int_in(payload.get(key), 0, high, f"invalid_{key}"))
    courier.declared_at = ot.now_utc()
    await session.flush()
    return {
        "success": True,
        "weekly_deliveries": courier.declared_weekly_deliveries,
        "active_days": courier.declared_active_days,
        "regular_clients": courier.declared_regular_clients,
    }


async def list_invited(session: AsyncSession, user: CurrentUser) -> dict[str, Any]:
    """listMyInvitedClients: his invited clients (first name only), their orders and his estimate."""
    courier = await _courier(session, user)
    clients = list(
        (
            await session.execute(
                select(User.id, User.full_name, User.referred_at, User.created_at)
                .where(signup_attribution(courier), User.deleted_at.is_(None))
                .order_by(func.coalesce(User.referred_at, User.created_at).desc())
                .limit(200)
            )
        ).all()
    )
    ids = [c[0] for c in clients]
    counts = (
        dict(
            (
                await session.execute(
                    select(Order.customer_id, func.count())
                    .where(Order.customer_id.in_(ids), Order.status == "delivered")
                    .group_by(Order.customer_id)
                )
            ).all()
        )
        if ids
        else {}
    )
    estimates = {
        e.customer_id: e.monthly_orders
        for e in (
            await session.execute(
                select(CourierClientEstimate).where(CourierClientEstimate.courier_id == courier.id)
            )
        ).scalars()
    }
    return {
        "success": True,
        "clients": [
            {
                "id": str(cid),
                "name": first_name(name),
                "joined": (referred or created).astimezone(TUNIS).date().isoformat()
                if (referred or created)
                else None,
                "delivered_orders": counts.get(cid, 0),
                "monthly_estimate": estimates.get(cid),
            }
            for cid, name, referred, created in clients
        ],
    }


async def set_client_estimate(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    """setInvitedClientEstimate: « ce client vous commande combien de fois par mois ? »."""
    courier = await _courier(session, user)
    customer_id = None
    try:
        customer_id = uuid.UUID(str(payload.get("client_id")).strip())
    except (TypeError, ValueError):
        raise OrderRefused(400, "invalid_client") from None
    invited = (
        await session.execute(select(User.id).where(User.id == customer_id, signup_attribution(courier)))
    ).scalar_one_or_none()
    if invited is None:
        raise OrderRefused(404, "client_not_found")
    value = _int_in(payload.get("monthly_orders"), 0, 60, "invalid_monthly_orders")
    if value is None:
        raise OrderRefused(400, "invalid_monthly_orders", min=0, max=60)
    stmt = insert(CourierClientEstimate).values(
        courier_id=courier.id, customer_id=customer_id, monthly_orders=value
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["courier_id", "customer_id"],
            set_={"monthly_orders": stmt.excluded.monthly_orders, "updated_at": func.now()},
        )
    )
    return {"success": True, "client_id": str(customer_id), "monthly_orders": value}


async def save_order_frequency(
    session: AsyncSession, user: CurrentUser, payload: dict[str, Any]
) -> dict[str, Any]:
    """saveMyOrderFrequency (customer): « vous vous faites livrer combien de fois par mois ? »."""
    value = _int_in(payload.get("monthly_orders"), 0, 60, "invalid_monthly_orders")
    row = await session.get(User, user.id, with_for_update=True)
    if row is None:
        raise OrderRefused(404, "user_not_found")
    row.declared_monthly_orders = value
    await session.flush()
    return {"success": True, "monthly_orders": value}


async def refresh_weather(session: AsyncSession, fetch: Any = None) -> dict[str, Any]:
    """Daily precipitation forecast for Sousse (Open-Meteo, free) kept in app_settings."""
    import httpx  # local: only the job needs it

    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=35.8256&longitude=10.6084"
        "&daily=precipitation_sum&timezone=Africa%2FTunis&forecast_days=16"
    )
    if fetch is None:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    else:
        data = await fetch(url)
    daily = dict(zip(data["daily"]["time"], data["daily"]["precipitation_sum"], strict=True))
    row = await session.get(AppSetting, WEATHER_KEY)
    value = {"daily": daily, "fetched_at": datetime.now(UTC).isoformat()}
    if row is None:
        session.add(AppSetting(key=WEATHER_KEY, value=value))
    else:
        row.value = value
    await session.flush()
    return {"days": len(daily)}
