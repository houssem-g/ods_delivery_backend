"""Vitrine paid extras (owner 17/10: « les ajouts en fonction du menu dans ODS… s'il n'y a pas d'ajout, on
fait rien »). Contract: ods-audit/livraison-1610/vitrine/options/ajouts/CONTRAT-AJOUTS.md.

The ONLY source is what the restaurant configured on the dish in ODS: its questions (« Suppléments »,
« Sauce », « Format ») and their priced answers, pushed with the menu (`products[].extras`, kept in
`vitrine_products.ods_extras`). No lexicon, no AI, nothing invented: a dish without a question has no
extra, a hand-made Vitrine has none. ODS has no « required », min/max nor per-answer availability: a group
with `multiple: false` takes one choice at most, `multiple: true` several, none is required.

Prices: the server always takes the price from the last catalog received (never the client's); the line's
unit price = the dish + its chosen extras.
"""

from decimal import Decimal
from typing import Any

from app.services import shops as shop_service

MAX_GROUPS = 8
MAX_CHOICES = 20
MAX_LINE_EXTRAS = 12
GROUP_NAME_MAX = 80
CHOICE_NAME_MAX = 60
KEY_MAX = 48
PRICE_MAX = Decimal("500")


def _clean(value: Any, limit: int) -> str:
    return shop_service.clean_text(value, limit)


def _key(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip()
    if not 1 <= len(key) <= KEY_MAX or not all(c.isalnum() or c in "-_" for c in key):
        return None
    return key


def _price(value: Any) -> Decimal | None:
    """0–500 DT, 3 decimals; None when not a number in range (the choice is dropped)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.001"))
    except Exception:  # any unreadable price
        return None
    if not amount.is_finite() or amount < 0 or amount > PRICE_MAX:
        return None
    return amount


def clean_groups(raw: Any) -> list[dict[str, Any]]:
    """The dish's extras as Delivery keeps them (catalog intake): [{id, name, multiple, choices: [{id, name,
    price}]}], 8 groups × 20 choices at most, a choice name once per group (the first wins), groups without
    a choice dropped. Pure."""
    out: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for g in raw if isinstance(raw, list) else []:
        g = g if isinstance(g, dict) else {}
        gid, name = _key(g.get("id")), _clean(g.get("name"), GROUP_NAME_MAX)
        if gid is None or not name or gid in seen_groups:
            continue
        choices, ids, names = [], set(), set()
        for c in g.get("choices") if isinstance(g.get("choices"), list) else []:
            c = c if isinstance(c, dict) else {}
            cid, cname, price = (
                _key(c.get("id")),
                _clean(c.get("name"), CHOICE_NAME_MAX),
                _price(c.get("price")),
            )
            if cid is None or not cname or price is None or cid in ids or cname.lower() in names:
                continue
            ids.add(cid)
            names.add(cname.lower())
            choices.append({"id": cid, "name": cname, "price": str(price)})
            if len(choices) >= MAX_CHOICES:
                break
        if not choices:
            continue
        seen_groups.add(gid)
        out.append({"id": gid, "name": name, "multiple": g.get("multiple") is True, "choices": choices})
        if len(out) >= MAX_GROUPS:
            break
    return out


def groups_of(product: Any) -> list[dict[str, Any]]:
    """The extras a product offers now: only a dish linked to ODS (a hand-made or unlinked one has none)."""
    if getattr(product, "ods_menu_id", None) is None:
        return []
    return clean_groups(getattr(product, "ods_extras", None))


def client_json(product: Any) -> list[dict[str, Any]]:
    """`product.options.extras` (CONTRAT-AJOUTS §2): the restaurant's own words in both languages."""
    return [
        {
            "key": g["id"],
            "name": {"fr": g["name"], "ar": g["name"]},
            "multiple": g["multiple"],
            "choices": [
                {
                    "key": c["id"],
                    "name": {"fr": c["name"], "ar": c["name"]},
                    "price": float(Decimal(c["price"])),
                }
                for c in g["choices"]
            ],
        }
        for g in groups_of(product)
    ]


def parse(raw: Any) -> list[str] | None:
    """A cart line's `extras` (shape only; None = 400 invalid_cart): the chosen choice keys, once each."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LINE_EXTRAS:
        return None
    out: list[str] = []
    for item in raw:
        key = _key(item)
        if key is None or key in out:
            return None
        out.append(key)
    return out


def signature(keys: list[str]) -> tuple[str, ...]:
    return tuple(sorted(keys))


def resolve(keys: list[str], groups: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """(the chosen extras in the dish's order, the refused keys). Refused: unknown / gone, or a second choice
    in a one-choice group (both of them are reported)."""
    where = {c["id"]: (g, c) for g in groups for c in g["choices"]}
    bad = [k for k in keys if k not in where]
    per_group: dict[str, list[str]] = {}
    for k in keys:
        if k in where:
            per_group.setdefault(where[k][0]["id"], []).append(k)
    for g in groups:
        picked = per_group.get(g["id"], [])
        if not g["multiple"] and len(picked) > 1:
            bad.extend(picked)
    if bad:
        return [], bad
    chosen = []
    for g in groups:
        for c in g["choices"]:
            if c["id"] in keys:
                chosen.append(
                    {
                        "key": c["id"],
                        "group": g["id"],
                        "group_name": g["name"],
                        "name": c["name"],
                        "price": c["price"],
                    }
                )
    return chosen, []


def total(chosen: list[dict[str, Any]]) -> Decimal:
    return sum((Decimal(e["price"]) for e in chosen), Decimal("0"))


def text(chosen: list[dict[str, Any]]) -> str:
    """« + Fromage (1.500 DT) · + Sauce harissa » (no price written for a free choice)."""
    return " · ".join(
        f"+ {e['name']} ({Decimal(e['price']):.3f} DT)" if Decimal(e["price"]) > 0 else f"+ {e['name']}"
        for e in chosen
    )
