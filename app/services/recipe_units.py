"""Recipe quantities — pure, no I/O: scaling to the number of people, the units a shop sells in, the
merge of several recipes, the rounding of a shopping list, the couffin's weight and the words the
courier reads.

Rules (owner 02/10 « kg et L, jamais g / ml / cl »; ETUDE T2):
- the recipe keeps the exact quantity to cook (`qty × servings / base_servings`);
- the shopping list adds the recipes up by ingredient FIRST, then rounds up to what one can buy
  (kg: 0,1 kg at least, by 0,05 kg under 1 kg, then by 0,1 kg; L: by 0,25 L; pieces and bunches: whole);
- a spoon (« c. à s. », « c. à c. ») is only written for what is at home; bought, it becomes 0,1 kg / 0,25 L;
- the weight of a piece or a bunch is an estimate for the couffin, never used as a price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any

SPOON = {
    "cas": Decimal("0.015"),
    "cac": Decimal("0.005"),
    "pincee": Decimal("0.0005"),
}  # kg (or L) in one spoon, for the weight only
KG_MIN, KG_STEP_SMALL, KG_STEP = Decimal("0.1"), Decimal("0.05"), Decimal("0.1")
L_STEP = Decimal("0.25")
UNIT_WORDS = {
    "fr": {"kg": "kg", "l": "L", "piece": ("pièce", "pièces"), "botte": ("botte", "bottes"),
           "cas": ("c. à s.", "c. à s."), "cac": ("c. à c.", "c. à c."), "pincee": ("pincée", "pincées")},
    "ar": {"kg": "كغ", "l": "ل", "piece": ("حبة", "حبات"), "botte": ("حزمة", "حزم"),
           "cas": ("ملعقة كبيرة", "ملاعق كبيرة"), "cac": ("ملعقة صغيرة", "ملاعق صغيرة"),
           "pincee": ("رشة", "رشات")},
}  # fmt: skip


@dataclass(frozen=True)
class Ing:
    """The part of an ingredient the computations need."""

    id: int | str  # the dictionary id, or "n:<key>" / "x:<id>:<unit>" for a line without a usable reference
    key: str
    name_fr: str
    name_ar: str
    unit: str  # kg | l | piece | botte
    aisle: str
    pantry: bool = False
    piece_kg: Decimal | None = None
    density: Decimal | None = None

    def name(self, lang: str) -> str:
        return self.name_ar if lang == "ar" else self.name_fr


def dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def scale(qty: Any, servings: int, base_servings: int) -> Decimal:
    return dec(qty) * Decimal(servings) / Decimal(max(1, base_servings))


def to_buy_unit(qty: Any, line_unit: str, ing: Ing) -> Decimal | None:
    """`qty` of a recipe line (in `line_unit`) in the unit the ingredient is bought in, unrounded;
    None when it cannot be said without guessing (a piece of something sold by kg with no known weight)."""
    q = dec(qty)
    if line_unit == ing.unit:
        return q
    if line_unit in SPOON:
        if ing.unit in ("kg", "l"):
            return q * SPOON[line_unit]
        return Decimal(1)  # a spoon of something sold by piece / bunch: one of it
    if (line_unit == "botte" and ing.unit == "piece") or (line_unit == "piece" and ing.unit == "botte"):
        return q
    if line_unit in ("piece", "botte") and ing.unit == "kg":
        return q * ing.piece_kg if ing.piece_kg else None
    if line_unit == "kg" and ing.unit in ("piece", "botte"):
        return q / ing.piece_kg if ing.piece_kg else None
    if line_unit == "kg" and ing.unit == "l":
        return q / (ing.density or Decimal(1))
    if line_unit == "l" and ing.unit == "kg":
        return q * (ing.density or Decimal(1))
    return None


def _ceil_to(q: Decimal, step: Decimal) -> Decimal:
    return (q / step).to_integral_value(rounding=ROUND_CEILING) * step


def round_buy(qty: Any, unit: str) -> Decimal:
    """What one asks the shop for (always ≥ the need)."""
    q = dec(qty)
    if q <= 0:
        return Decimal(0)
    if unit == "kg":
        if q <= KG_MIN:
            return KG_MIN
        return _ceil_to(q, KG_STEP_SMALL if q < 1 else KG_STEP).normalize()
    if unit == "l":
        return _ceil_to(q, L_STEP).normalize()
    return Decimal(math.ceil(q))


def weight_kg(qty: Any, unit: str, ing: Ing) -> Decimal | None:
    q = dec(qty)
    if unit == "kg":
        return q
    if unit == "l":
        return q * (ing.density or Decimal(1))
    if unit in ("piece", "botte"):
        return q * ing.piece_kg if ing.piece_kg else None
    if unit in SPOON:
        return q * SPOON[unit]
    return None


def number(q: Any, lang: str) -> str:
    """0,35 (fr) / 0.35 (ar), at most 3 decimals, no trailing zero."""
    d = dec(q).quantize(Decimal("0.001"))
    text = f"{d:f}".rstrip("0").rstrip(".") if "." in f"{d:f}" else f"{d:f}"
    return text if lang == "ar" else text.replace(".", ",")


def unit_word(q: Any, unit: str, lang: str) -> str:
    words = UNIT_WORDS["ar" if lang == "ar" else "fr"][unit]
    if isinstance(words, str):
        return words
    return words[0] if dec(q) <= 1 else words[1]


def shown_qty(q: Any, unit: str) -> Decimal:
    """What a person can measure (QA 22/10 r2-8: « 0,233 pièce », « 11,667 pièces », « 1,167 c. à s. »):
    pieces, bunches and pinches whole (at least 1), spoons by halves (at least ½); kg and L unchanged. Words
    only: the computations keep the exact quantity."""
    d = dec(q)
    if d <= 0:
        return d
    if unit in ("piece", "botte", "pincee"):
        return max(Decimal(1), d.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if unit in ("cas", "cac"):
        return max(Decimal("0.5"), (d * 2).quantize(Decimal(1), rounding=ROUND_HALF_UP) / 2)
    return d


def fmt_qty(q: Any, unit: str, lang: str = "fr") -> str:
    """« 0,35 kg », « 1,5 L », « 4 pièces », « 1 botte » / « 0.35 كغ », « 4 حبات »."""
    q = shown_qty(q, unit)
    return f"{number(q, lang)}\u00a0{unit_word(q, unit, lang)}"


def merge(needs: list[tuple[Ing, Decimal]]) -> list[tuple[Ing, Decimal]]:
    """[(ingredient, qty in its buying unit)] summed by ingredient, in first-seen order."""
    total: dict[int | str, tuple[Ing, Decimal]] = {}
    for ing, q in needs:
        prev = total.get(ing.id)
        total[ing.id] = (ing, (prev[1] if prev else Decimal(0)) + q)
    return list(total.values())
