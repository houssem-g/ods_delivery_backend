"""What a push looks like on the phone: THE builder of every push's visible text (owner, 17/10/2026).

Every push goes through `push.build_multicast` → `render`. The in-app rows (Notifications page) keep
their own texts; only what the lock screen shows is built here, from the notice's type, its metadata
and the order's « card » (shop, areas, amount, dishes, photo, test flag).

Rules (« Fiche claire », direction A of ods-audit/livraison-1610/notifications):
- title ≤ 40 characters (an Android lock screen): the action first, then the essential
  (« Nouvelle course · 3,2 km · Khezama »);
- first body line ≤ 80 (the folded notification): shop → destination + amount; the next lines are
  the expanded view (Android BigText, iOS, web): the dishes with their « Sans »;
- no decorative emoji; a test order says « Test » at the END of the title (always visible, the
  title is shortened first), never « QA TEST [Vitrine] » ahead. Who RECEIVES a test order is decided
  by dispatch (QA accounts only), never here;
- « 45.500 DT » (3 decimals, as the app), « 3,2 km »; Arabic: standard, « د.ت », « كم »;
- accent colour of the side (violet courier, green customer, red for the door alarm);
- one notification per order on screen (tag = the order), replaced by the next step; several offers
  become « 3 offres reçues · dès 4.000 DT ».

Works without any app update: only fields the installed shells already honour (title, body, color,
image, tag, notification_count, the existing channels; APNs title / subtitle / body / thread-id; web
push title, body, icon, badge, image, tag, renotify, dir, lang). The status-bar icon asked for
(ic_stat_ods, the logo) needs the next shell build: until then Android shows the manifest's bag.
"""

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.order_texts import minutes_ar, short_name

log = logging.getLogger("odsd.push_design")

TITLE_MAX = 40
LINE_MAX = 80
BODY_MAX = 400
LINES_MAX = 4
TITLE_HARD_MAX = 60  # a generic title longer than this is cut (the OS would cut it anyway)

COURIER_COLOR = "#7C3AED"  # the courier side's accent (html[data-mode="courier"])
CUSTOMER_COLOR = "#00D9A5"  # ODS green
URGENT_COLOR = "#E5484D"  # the door alarm only

SEP = " · "
LTR_START, LTR_END = "\u2068", "\u2069"  # first-strong isolate: bidi-safe Latin inside Arabic
TEST_MARK = {"fr": "Test", "ar": "تجربة"}

# Pictographs and symbols used as decoration (🎯 ✅ ❌ ⚠️ ⌛ ⏱️ ➖ ⭐ …). Arrows (→) are kept.
_EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⌀-⏿⬀-⯿─-◿️‍⃣]")
_TEST_WORDS = re.compile(r"\bQA TEST\b:?\s*|(?<![\w-])PW-[A-Za-z0-9_-]*\s*", re.IGNORECASE)
_TEST_ORDER = re.compile(r"QA TEST|\bPW-", re.IGNORECASE)  # same as orders.TEST_ORDER_RE
_VITRINE_LABEL = re.compile(r"\[Vitrine\]\s*", re.IGNORECASE)
_TOTAL_LINE = re.compile(r"^\s*Total articles\b", re.IGNORECASE)
_PRICE_TAIL = re.compile(r"\s+—\s+\d+(?:[.,]\d+)?\s*DT\b")


# ─────────────────────────────── small text helpers ───────────────────────────────


def strip_emoji(text: str) -> str:
    return " ".join(_EMOJI.sub(" ", text or "").split())


def strip_test(text: str) -> str:
    """« QA TEST [Vitrine] Coffee and Tea » → « Coffee and Tea » (line breaks kept)."""
    lines = []
    for line in str(text or "").split("\n"):
        line = _VITRINE_LABEL.sub("", _TEST_WORDS.sub("", line))
        lines.append(" ".join(line.split()))
    return "\n".join(lines).strip()


def is_test_text(*texts: str | None) -> bool:
    return any(_TEST_ORDER.search(t or "") for t in texts)


def truncate(text: str, limit: int) -> str:
    """At most `limit` characters, cut on a word with « … » (never mid-word when avoidable)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ·,;:-—→") + "…"


def fit(candidates: list[str], limit: int) -> str:
    """The first candidate that fits, else the last one cut."""
    for text in candidates:
        if text and len(text) <= limit:
            return text
    return truncate(next((c for c in reversed(candidates) if c), ""), limit)


def with_mark(candidates: list[str], test: bool, lang: str, limit: int = TITLE_MAX) -> str:
    """The title, « · Test » at the end of a test order's title, always visible."""
    if not test:
        return fit(candidates, limit)
    mark = SEP + TEST_MARK[lang]
    return fit(candidates, limit - len(mark)) + mark


def iso(text: str, lang: str) -> str:
    """A Latin name inside an Arabic text, isolated so the bidi algorithm keeps it in one piece
    (« Karim T. · ~15 دق » was shown « دق Karim T. · ~15 »)."""
    if lang != "ar" or not text or not re.search(r"[A-Za-z0-9]", text):
        return text
    return f"{LTR_START}{text}{LTR_END}"


def money(value: Any) -> str:
    return f"{float(value):.3f}"


def km(value: Any, lang: str) -> str:
    text = f"{float(value):.1f}"
    return text if lang == "ar" else text.replace(".", ",")


def offers_ar(n: int) -> str:
    if n == 2:
        return "عرضان"
    if 3 <= n <= 10:
        return f"{n} عروض"
    return f"{n} عرضاً"


def articles_more(n: int, lang: str) -> str:
    if lang == "fr":
        return f"+ {n} autre article" if n == 1 else f"+ {n} autres articles"
    if n == 1:
        return "+ منتج آخر"
    if n == 2:
        return "+ منتجان آخران"
    return f"+ {n} منتجات أخرى" if n <= 10 else f"+ {n} منتجاً آخر"


# ─────────────────────────────── areas (Sousse first) ───────────────────────────────

# (French name shown, Arabic name, spellings found in addresses). Neighbourhoods before towns: the
# most precise one in the address wins.
AREAS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Khezama", "الخزامة", ("khezama", "khzema", "khzama", "khezema", "الخزامة")),
    ("Sahloul", "سهلول", ("sahloul", "sahlol", "سهلول")),
    ("Kantaoui", "القنطاوي", ("kantaoui", "el kantaoui", "القنطاوي")),
    ("Hammam Sousse", "حمام سوسة", ("hammam sousse", "hammam-sousse", "حمام سوسة")),
    ("Bouhsina", "بوحسينة", ("bouhsina", "bou hsina", "بوحسينة")),
    ("Sidi Abdelhamid", "سيدي عبد الحميد", ("sidi abdelhamid", "sidi abdel hamid", "سيدي عبد الحميد")),
    ("Riadh", "الرياض", ("riadh", "erriadh", "cite riadh", "الرياض")),
    ("Zaouiet Sousse", "زاوية سوسة", ("zaouiet sousse", "zaouia", "زاوية سوسة")),
    ("Trocadéro", "الطروكاديرو", ("trocadero", "الطروكاديرو")),
    ("Bab Jedid", "باب جديد", ("bab jedid", "bab jdid", "باب جديد")),
    ("Boujaafar", "بوجعفر", ("boujaafar", "bou jaafar", "بوجعفر")),
    ("Corniche", "الكورنيش", ("corniche", "الكورنيش")),
    ("Cité Ezzouhour", "حي الزهور", ("ezzouhour", "zouhour", "حي الزهور")),
    ("Médina", "المدينة العتيقة", ("medina", "المدينة العتيقة")),
    ("Msaken", "مساكن", ("msaken", "m'saken", "مساكن")),
    ("Kalâa Kebira", "القلعة الكبرى", ("kalaa kebira", "kalaa kbira", "القلعة الكبرى")),
    ("Kalâa Seghira", "القلعة الصغرى", ("kalaa seghira", "kalaa sghira", "القلعة الصغرى")),
    ("Akouda", "أكودة", ("akouda", "أكودة")),
    ("Chott Meriem", "شط مريم", ("chott meriem", "chott mariem", "شط مريم")),
    ("Ksibet Thrayet", "قصيبة الثريات", ("ksibet thrayet", "قصيبة الثريات")),
    ("Messaadine", "المسعدين", ("messaadine", "المسعدين")),
    ("Kondar", "كندار", ("kondar", "كندار")),
    ("Hergla", "هرقلة", ("hergla", "هرقلة")),
    ("Enfidha", "النفيضة", ("enfidha", "النفيضة")),
    ("Sidi Bou Ali", "سيدي بوعلي", ("sidi bou ali", "سيدي بوعلي")),
    ("Sousse", "سوسة", ("sousse", "سوسة")),
    ("Monastir", "المنستير", ("monastir", "المنستير")),
    ("Mahdia", "المهدية", ("mahdia", "المهدية")),
    ("Hammamet", "الحمامات", ("hammamet", "الحمامات")),
    ("Nabeul", "نابل", ("nabeul", "نابل")),
    ("Kairouan", "القيروان", ("kairouan", "القيروان")),
    ("Sfax", "صفاقس", ("sfax", "صفاقس")),
    ("Tunis", "تونس", ("tunis", "تونس")),
)


def _plain(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower())
    return " " + " ".join("".join(c for c in folded if not unicodedata.combining(c)).split()) + " "


def area_of(*places: str | None) -> tuple[str, str]:
    """(French, Arabic) name of the most precise known area in `places` (address, city,
    governorate, in that order); else the city as written; ("", "") when nothing usable."""
    for place in places:
        plain = _plain(strip_test(place or ""))
        for fr, ar, spellings in AREAS:
            if any(re.search(rf"(?<![\w]){re.escape(s)}(?![\w])", plain) for s in spellings):
                return fr, ar
    for place in places[1:]:  # a city we do not know: shown as written
        name = truncate(strip_test(place or ""), 18)
        if name:
            return name, name
    return "", ""


# ─────────────────────────────── the order's card ───────────────────────────────


@dataclass(frozen=True)
class OrderCard:
    """What every push of an order may show about it (loaded once per notice, or per dispatch)."""

    shop: str = ""
    shop_area: tuple[str, str] = ("", "")
    dest_area: tuple[str, str] = ("", "")
    goods: Decimal | None = None  # the purchase: the cart's total (exact) or the customer's estimate
    goods_exact: bool = False
    lines_fr: tuple[str, ...] = ()
    lines_ar: tuple[str, ...] = ()
    more: int = 0  # lines not shown
    image: str | None = None
    test: bool = False
    offers: int = 0  # pending offers (new_offer only)
    best_fee: Decimal | None = None


def _cart_lines(cart: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    fr, ar = [], []
    for line in (cart or {}).get("lines") or []:
        if not isinstance(line, dict):
            continue
        name = truncate(strip_test(str(line.get("name") or "")), 32)
        head = f"{line.get('qty') or 1}× {name}"
        mods = [m for m in line.get("mods") or [] if isinstance(m, dict)]
        mods_fr = ", ".join(str(m.get("label_fr") or "") for m in mods if m.get("label_fr"))
        mods_ar = "، ".join(str(m.get("label_ar") or m.get("label_fr") or "") for m in mods)
        if not mods and line.get("mods_text"):
            mods_fr = mods_ar = str(line["mods_text"])
        # the paid extras (« + Fromage »), names only: the price is the restaurant's business
        adds = [f"+ {e['name']}" for e in line.get("extras") or [] if isinstance(e, dict) and e.get("name")]
        if adds:
            mods_fr = ", ".join(p for p in (mods_fr, *adds) if p)
            mods_ar = "، ".join(p for p in (mods_ar, *(iso(a, "ar") for a in adds)) if p)
        fr.append(truncate(f"{head}{SEP}{mods_fr}" if mods_fr else head, 60))
        # the Latin « 2× Burger » isolated: in an Arabic line it keeps its order (« 2× » first)
        ar_head = f"{LTR_START}{head}{LTR_END}"
        ar.append(truncate(f"{ar_head}{SEP}{mods_ar}" if mods_ar.strip("، ") else ar_head, 62))
    return fr, ar


def _text_lines(items_text: str | None) -> list[str]:
    rows = []
    for raw in str(items_text or "").split("\n"):
        if _VITRINE_LABEL.search(raw):
            continue  # « [Vitrine] <shop> »: the shop is said elsewhere
        row = _PRICE_TAIL.sub("", strip_test(raw)).strip(" ·-")
        if row and not _TOTAL_LINE.match(row):
            rows.append(truncate(row, 60))
    return rows


def card_of(
    order: Any,
    stop: Any | None,
    photo: str | None = None,
    offers: int = 0,
    best_fee: Decimal | None = None,
) -> OrderCard:
    """The card of `order` (an Order row) and its first shop (an OrderStop row). Pure."""
    fr, ar = _cart_lines(order.cart)
    if not fr:
        fr = ar = _text_lines(order.items_text)
    goods = order.estimated_price if order.estimated_price is not None else order.budget_max
    return OrderCard(
        shop=truncate(strip_test(stop.name if stop is not None else ""), 32),
        shop_area=area_of(stop.address, stop.city, stop.governorate) if stop is not None else ("", ""),
        dest_area=area_of(order.delivery_address, order.delivery_city, order.delivery_governorate),
        goods=goods if goods else None,
        goods_exact=bool(order.cart) or order.source == "partner",
        lines_fr=tuple(fr[:LINES_MAX]),
        lines_ar=tuple(ar[:LINES_MAX]),
        more=max(0, len(fr) - LINES_MAX),
        image=photo if isinstance(photo, str) and photo.startswith("https://") else None,
        test=is_test_text(order.items_text),  # the same rule as dispatch (QA accounts only)
        offers=offers,
        best_fee=best_fee,
    )


async def load_card(session: AsyncSession, order_id: Any, *, with_offers: bool = False) -> OrderCard | None:
    """The card of an order, None when it cannot be read: a push never fails for its looks."""
    try:
        oid = order_id if isinstance(order_id, uuid.UUID) else uuid.UUID(str(order_id))
    except (TypeError, ValueError):
        return None
    from app.models import Order, OrderOffer, OrderStop, Shop, VitrineProduct
    from app.services.shops import photo_url_of

    try:
        async with session.begin_nested():
            order = await session.get(Order, oid)
            if order is None:
                return None
            stop = (
                await session.execute(select(OrderStop).where(OrderStop.order_id == oid, OrderStop.seq == 0))
            ).scalar_one_or_none()
            photo = None
            first = next(iter((order.cart or {}).get("lines") or []), None)
            if isinstance(first, dict) and first.get("product_id"):
                key = (
                    await session.execute(
                        select(VitrineProduct.photo_key).where(
                            VitrineProduct.id == uuid.UUID(str(first["product_id"]))
                        )
                    )
                ).scalar_one_or_none()
                photo = photo_url_of(key)
            shop_id = (stop.shop_id if stop is not None else None) or order.vitrine_shop_id
            if photo is None and shop_id is not None:
                shop_photo = select(Shop.photo_key).where(Shop.id == shop_id)
                photo = photo_url_of((await session.execute(shop_photo)).scalar_one_or_none())
            offers, best = 0, None
            if with_offers:
                offers, best = (
                    await session.execute(
                        select(func.count(), func.min(OrderOffer.proposed_fee)).where(
                            OrderOffer.order_id == oid, OrderOffer.status == "pending"
                        )
                    )
                ).one()
            return card_of(order, stop, photo, int(offers or 0), best)
    except Exception:
        log.exception("push card of order %s not built", order_id)
        return None


# ─────────────────────────────── the rendered push ───────────────────────────────


@dataclass(frozen=True)
class PushView:
    title: str
    body: str  # Android / web: first line folded, every line expanded
    ios_subtitle: str | None
    ios_body: str
    color: str
    image: str | None = None
    count: int | None = None  # Android notification_count (several offers)
    renotify: bool = False  # web: a replacement on the same tag rings again
    lang: str = "fr"


def _role(type_: str, metadata: dict[str, Any]) -> str | None:
    from app.services.push import recipient_role  # push imports this module

    return recipient_role(type_, metadata)


def _join(first: str, lines: list[str] | tuple[str, ...], line_max: int = LINE_MAX) -> str:
    rows = [truncate(first, line_max)] if first else []
    rows += [truncate(r, line_max) for r in lines if r]
    text = "\n".join(rows)
    return text if len(text) <= BODY_MAX else truncate(text.replace("\n", " · "), BODY_MAX)


def _lines(card: OrderCard, lang: str) -> list[str]:
    rows = list(card.lines_fr if lang == "fr" else card.lines_ar)
    if card.more:
        rows.append(articles_more(card.more, lang))
    return rows


def _goods(card: OrderCard, lang: str) -> str:
    if card.goods is None:
        return ""
    amount = ("" if card.goods_exact else "≈ ") + money(card.goods)
    return f"achat {amount} DT" if lang == "fr" else f"شراء {amount} د.ت"


def _route(card: OrderCard, lang: str, shop_max: int = 28) -> str:
    shop = truncate(card.shop, shop_max)
    dest = card.dest_area[0 if lang == "fr" else 1]
    if lang == "fr":
        return f"{shop} → {dest}" if shop and dest else shop or (f"Livraison à {dest}" if dest else "")
    if shop and dest:
        return f"من {iso(shop, lang)} إلى {dest}"
    return shop or (f"التوصيل إلى {dest}" if dest else "")


def _new_order(msg: Any, card: OrderCard, lang: str) -> tuple[list[str], str, list[str]]:
    dist = msg.metadata.get("distance_km")
    d = (f"{km(dist, 'fr')} km" if lang == "fr" else f"{km(dist, 'ar')} كم") if dist is not None else ""
    area = card.shop_area[0 if lang == "fr" else 1]
    if msg.metadata.get("preferred_courier"):
        heads = ["Course de votre client", "Votre client"] if lang == "fr" else ["طلب من حريفك"]
    else:
        heads = ["Nouvelle course", "Course"] if lang == "fr" else ["طلب جديد", "طلب"]
    titles = [SEP.join(p for p in (h, d, area) if p) for h in heads]
    titles += [SEP.join(p for p in (h, d) if p) for h in heads]
    first = SEP.join(p for p in (_route(card, lang), _goods(card, lang)) if p)
    return titles, first, _lines(card, lang)


def _new_offer(msg: Any, card: OrderCard, lang: str) -> tuple[list[str], str, list[str]]:
    m = msg.metadata
    name = iso(short_name(m.get("courier_name")), lang) or ("Un livreur" if lang == "fr" else "مندوب")
    eta = m.get("eta_minutes")
    eta_t = (f"~{eta} min" if lang == "fr" else f"~{eta} دق") if eta else ""
    fee = m.get("proposed_fee")
    edited = m.get("kind") == "offer_updated"
    if card.offers >= 2 and not edited and card.best_fee is not None:
        best = money(card.best_fee)
        titles = (
            [f"{card.offers} offres reçues · dès {best} DT", f"{card.offers} offres reçues"]
            if lang == "fr"
            else [f"{offers_ar(card.offers)} · ابتداءً من {best} د.ت", offers_ar(card.offers)]
        )
    else:
        head = (
            ("Offre modifiée" if edited else "Nouvelle offre")
            if lang == "fr"
            else ("تم تعديل العرض" if edited else "عرض جديد")
        )
        titles = [SEP.join(p for p in (head, name, eta_t) if p), SEP.join((head, name)), head]
    if lang == "fr":
        price = f"Livraison {money(fee)} DT + vos achats" if fee is not None else "Livraison + vos achats"
        if card.offers >= 2 and not edited:
            price = f"{name} : {price[:1].lower()}{price[1:]}"
        tail = "Touchez pour comparer et choisir." if card.offers >= 2 else "Touchez pour voir et accepter."
    else:
        price = f"التوصيل {money(fee)} د.ت + مشترياتك" if fee is not None else "التوصيل + مشترياتك"
        if card.offers >= 2 and not edited:
            price = f"{name}: {price}"
        tail = "المس للمقارنة والاختيار." if card.offers >= 2 else "المس للاطلاع والقبول."
    first = SEP.join(p for p in (price, iso(truncate(card.shop, 24), lang)) if p)
    return titles, first, [tail]


def _accepted(msg: Any, card: OrderCard, lang: str) -> tuple[list[str], str, list[str]]:
    head = "Offre acceptée" if lang == "fr" else "تم قبول عرضك"
    titles = [SEP.join(p for p in (head, iso(card.shop, lang)) if p), head]
    area = card.shop_area[0 if lang == "fr" else 1]
    dest = card.dest_area[0 if lang == "fr" else 1]
    if lang == "fr":
        go = f"Allez au magasin ({area})" if area else "Allez au magasin"
        first = SEP.join(p for p in (go, f"livraison à {dest}" if dest else "") if p)
    else:
        go = f"توجّه إلى المتجر ({area})" if area else "توجّه إلى المتجر"
        first = SEP.join(p for p in (go, f"التوصيل إلى {dest}" if dest else "") if p)
    return titles, first, _lines(card, lang)


def _minutes_left(metadata: dict[str, Any], now: datetime) -> int | None:
    raw = metadata.get("deadline_at")
    try:
        deadline = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return max(1, round((deadline - now).total_seconds() / 60))


def _at_the_door(
    msg: Any, card: OrderCard | None, lang: str, now: datetime
) -> tuple[list[str], str, list[str]]:
    m = msg.metadata
    last = int(m.get("attempt") or 1) > 1
    mins = _minutes_left(m, now)
    shop = card.shop if card else ""
    if lang == "fr":
        titles = (
            ["Dernier appel : livreur à votre porte", "Dernier appel : livreur à la porte"]
            if last
            else ["Votre livreur est devant chez vous", "Livreur devant chez vous"]
        )
        # the deadline first: the folded line keeps it
        first = (
            f"Répondez sous {mins} min : appelez-le ou confirmez"
            if mins
            else ("Appelez-le ou confirmez que vous êtes là")
        )
        rest = ["Il a avancé l'argent de vos achats" + (f" chez {shop}." if shop else ".")]
        if "incident" in (msg.body_fr or ""):
            rest.append("Un incident est enregistré : il est retiré si vous répondez.")
    else:
        titles = ["نداء أخير: المندوب أمام بابك"] if last else ["المندوب أمام منزلك"]
        first = f"أجب خلال {minutes_ar(mins)}: اتصل به أو أكّد وجودك" if mins else "اتصل به أو أكّد أنك موجود"
        rest = ["دفع المندوب ثمن مشترياتك" + (f" من {iso(shop, lang)}." if shop else ".")]
        if "حادثة" in (msg.body_ar or ""):
            rest.append("سُجّلت حادثة عدم رد: تُسحب إن رددت الآن.")
    return titles, first, rest


def _generic(msg: Any, lang: str) -> tuple[list[str], str, list[str]]:
    title = strip_test(strip_emoji(msg.title_fr if lang == "fr" else msg.title_ar))
    body = strip_test(msg.body_fr if lang == "fr" else msg.body_ar)
    rows = [" ".join(strip_emoji(r).split()) if i == 0 else r for i, r in enumerate(body.split("\n"))]
    rows = [r for r in rows if r]
    return [title], rows[0] if rows else "", rows[1:]


TEMPLATES = {"new_order": _new_order, "new_offer": _new_offer, "order_accepted": _accepted}


def render(msg: Any, locale: str, now: datetime | None = None) -> PushView:
    """The visible push of `msg` (a push.PushMessage) for a device in `locale` ('fr', else Arabic)."""
    lang = "fr" if locale == "fr" else "ar"
    card: OrderCard | None = getattr(msg, "card", None)
    role = _role(msg.type, msg.metadata)
    color = COURIER_COLOR if role == "courier" else CUSTOMER_COLOR
    image, count = None, None
    door = msg.type == "emergency_contact" and msg.metadata.get("stage") == "alert"
    template = TEMPLATES.get(msg.type)
    is_courier_offer_event = msg.type == "order_accepted" and role != "courier"
    if door:
        titles, first, rest = _at_the_door(msg, card, lang, now or datetime.now(UTC))
    elif template is not None and card is not None and not is_courier_offer_event:
        titles, first, rest = template(msg, card, lang)
    else:
        titles, first, rest = _generic(msg, lang)
    if msg.type == "emergency_contact":
        color = URGENT_COLOR
    if msg.type == "new_offer" and card is not None:
        image = card.image
        count = card.offers if card.offers >= 2 else None
    elif msg.type == "new_order" and card is not None and not card.lines_fr:
        image = card.image  # a photo only when it hides no dish (Android shows one or the other)
    # the order's own rule (its items_text, as dispatch); without a card, the notice's words
    test = (
        card.test if card is not None else is_test_text(msg.title_fr, msg.title_ar, msg.body_fr, msg.body_ar)
    )
    # built here: the action and the essential within what a lock screen shows
    designed = door or (template is not None and card is not None and not is_courier_offer_event)
    limit = TITLE_MAX if designed or test else TITLE_HARD_MAX
    title = with_mark(titles, test, lang, limit) or "ODS Delivery"
    line_max = LINE_MAX if designed else BODY_MAX
    split = designed and bool(first) and bool(rest)
    return PushView(
        title=title,
        body=_join(first, rest, line_max),
        ios_subtitle=truncate(first, LINE_MAX) if split else None,
        ios_body=_join("", rest, line_max) if split else _join(first, rest, line_max),
        color=color,
        image=image,
        count=count,
        renotify=msg.type in ("new_order", "new_offer", "emergency_contact"),
        lang=lang,
    )
