"""Vitrine « ce que j'enlève » (owner 17/10): the removals a customer can ask on a dish — « Peu de
harissa », « Sans oignon » — computed for EVERY Vitrine, without the restaurant doing anything and
without any AI call (deterministic rules, 0 $). Contract: ods-audit/.../vitrine/options/CONTRAT-OPTIONS.md.

Where the ingredients come from:
1. the dish's recipe in ODS (``vitrine_products.ods_ingredients``, pushed in the catalog) when it has
   one — each ingredient read through the lexicon; ODS can say « never » (``removable: false``) or
   « always » (``removable: true``) for one;
2. otherwise (and for the hand-made Vitrines) the lexicon read over the dish's name and description,
   plus a template of what a dish of that type carries in Tunisia (makloub, chapati, pizza, café…).

Rules that keep it sensible (expert review 17/10, REVUE-EXPERTS.md):
- only the ingredients of the lexicon are ever proposed (packaging, flour, coffee… never);
- each dish type has a whitelist of removable keys (no « sans fromage » on a pizza, no « sans sucre »
  baked in a tiramisu, no « sans lait » in a latte); bottles, water, sodas: nothing;
- an ingredient named in the dish's title is part of the dish: dropped, or « Peu de » only for a
  condiment (« Burger piquant » → « Peu de harissa »);
- at most 6 per dish, ranked by what the customers of this dish, then of its category, removed most
  (``vitrine_option_stats``, aggregated counts, no customer), then by the lexicon's order (the order
  of the requests heard at a Tunisian counter, sugar first for the drinks).

Phase 2 (paid extras) only has its place in the shapes (``options.extras``, ``mods[].price_delta``).
"""

import logging
import re
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ApiError

logger = logging.getLogger(__name__)

MAX_SHOWN = 6
MAX_MODS = 8
MAX_ADDED = 6
MAX_INGREDIENTS = 30
INGREDIENT_NAME_MAX = 60
CUSTOM_NAME_MAX = 40
LEVELS = ("less", "none")
# shops whose products are dishes (a supermarket's « Lait 1 L » gets nothing)
FOOD_SHOPS = {None, "", "restaurant", "cafe", "bakery", "other"}


# ─────────────────────────────── text ───────────────────────────────

_MARKS = re.compile(r"[̀-ًͯ-ٰٟـ]")
_NON_WORD = re.compile(r"[^0-9a-z؀-ۿ]+")
_ARABIC_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه"})


def norm(value: Any) -> str:
    """lower case, œ → oe, accents and harakat removed, Arabic letters folded, words separated by one
    space, padded with spaces (« oignon » is searched as « ␣oignon␣ »)."""
    text = "" if value is None else str(value)
    text = text.lower().replace("œ", "oe").replace("æ", "ae").replace("’", "'")
    text = _MARKS.sub("", unicodedata.normalize("NFD", text)).translate(_ARABIC_FOLD)
    text = _NON_WORD.sub(" ", text).strip()
    words = [w[:-1] if len(w) > 3 and w[-1] in "sx" and w.isascii() else w for w in text.split()]
    return f" {' '.join(words)} " if words else " "


def has(text: str, phrase: str) -> bool:
    """``phrase`` (already normalized, no padding) appears as whole words in ``text`` (normalized)."""
    return f" {phrase} " in text


# ─────────────────────────────── the lexicon ───────────────────────────────


@dataclass(frozen=True)
class Ingredient:
    key: str
    fr: str  # the chip: « Harissa »
    noun: str  # in the labels: « Sans harissa », « Peu de harissa »
    ar: str  # « هريسة » → « بدون هريسة »
    ar_def: str  # « الهريسة » → « قليل من الهريسة »
    less: bool  # « Peu de » makes sense
    condiment: bool  # named in the title: still « Peu de »
    aliases: tuple[str, ...]
    blockers: tuple[str, ...] = ()  # « double concentré de tomate » is not a fresh tomato

    def labels(self) -> dict[str, dict[str, str]]:
        out = {"none": {"fr": f"Sans {self.noun}", "ar": f"بدون {self.ar}"}}
        if self.less:
            elided = self.noun[0] in "aeiouyéèêâîôœ"
            out["less"] = {
                "fr": f"Peu d'{self.noun}" if elided else f"Peu de {self.noun}",
                "ar": f"قليل من {self.ar_def}",
            }
        return out


def _i(key, fr, noun, ar, ar_def, less, condiment, aliases, blockers=()) -> Ingredient:
    return Ingredient(key, fr, noun, ar, ar_def, less, condiment, tuple(aliases), tuple(blockers))


# The order = what is asked most at a Tunisian counter (sugar first: the drinks).
# fmt: off
LEXICON: tuple[Ingredient, ...] = (
    _i("sucre", "Sucre", "sucre", "سكر", "السكر", True, True,
       ["sucre", "sucre en poudre", "sucre semoule", "sucre en sachet", "sucre glace", "sokkor", "sokor",
        "sukkar", "سكر"]),
    _i("harissa", "Harissa", "harissa", "هريسة", "الهريسة", True, True,
       ["harissa", "hrissa", "harisa", "hrisa", "harissa arbi", "هريسه", "هريسة"]),
    _i("oignon", "Oignon", "oignon", "بصل", "البصل", False, False,
       ["oignon", "ognon", "onion", "oignon rouge", "oignon blanc", "oignon frit", "bsal", "basla", "بصل"]),
    _i("mayonnaise", "Mayonnaise", "mayonnaise", "مايونيز", "المايونيز", True, True,
       ["mayonnaise", "mayo", "mayonaise", "mayonnaisse", "mayonez", "مايونيز"]),
    _i("frites", "Frites", "frites", "بطاطا مقلية", "البطاطا المقلية", False, False,
       ["frite", "pomme frite", "frite surgelee", "frite maison", "batata", "فريت", "بطاطا مقليه"]),
    _i("salade", "Salade", "salade", "سلطة", "السلطة", False, False,
       ["salade", "salade verte", "laitue", "laitue piece", "salata", "خس", "سلطه"],
       ["fruit", "mechouia", "tunisienne", "cesar", "caesar"]),
    _i("fromage", "Fromage", "fromage", "جبن", "الجبن", False, False,
       ["fromage", "fromage rape", "gruyere", "gruyere rape", "emmental", "mozzarella", "mozarella", "mozza",
        "cheddar", "fromage cheddar", "parmesan", "burrata", "feta", "edam", "vache qui rit", "cheese",
            "cacio",
        "jben", "جبن", "فرماج"],
       ["mascarpone", "ricotta", "fromagere"]),
    _i("olives", "Olives", "olives", "زيتون", "الزيتون", False, False,
       ["olive", "olive noire", "olive verte", "zitoun", "zitouna", "زيتون"], ["huile"]),
    _i("piment", "Piment", "piment", "فلفل حار", "الفلفل الحار", True, True,
       ["piment", "piment fort", "piment vert", "piment rouge", "piment grille", "felfel har", "فلفل حار"]),
    _i("tomate", "Tomate", "tomate", "طماطم", "الطماطم", False, False,
       ["tomate", "tomate fraiche", "tomate cerise", "tmatem", "طماطم"],
       ["concentre", "sauce", "coulis", "puree", "seche"]),
    _i("glacons", "Glaçons", "glaçons", "ثلج", "الثلج", True, False, ["glacon", "glace pilee",
        "ice cube", "ثلج"]),
    _i("thon", "Thon", "thon", "تن", "التن", False, False, ["thon", "thon a l huile", "tuna", "تن", "تونه"]),
    _i("oeuf", "Œuf", "œuf", "بيض", "البيض", False, False, ["oeuf", "oeuf dur", "oeuf au plat", "egg",
        "بيض", "بيضه"]),
    _i("sauce_blanche", "Sauce blanche", "sauce blanche", "صلصة بيضاء", "الصلصة البيضاء", True, True,
       ["sauce blanche", "sauce yaourt", "sauce a l ail", "صلصه بيضاء"]),
    _i("sauce", "Sauce", "sauce", "صلصة", "الصلصة", True, True,
       ["sauce algerienne", "sauce samourai", "sauce biggy", "sauce barbecue", "sauce bbq", "sauce piquante",
        "sauce burger", "sauce maison"]),
    _i("ketchup", "Ketchup", "ketchup", "كاتشب", "الكاتشب", True, True, ["ketchup", "ketchap", "كاتشب"]),
    _i("moutarde", "Moutarde", "moutarde", "خردل", "الخردل", True, True, ["moutarde", "mustard", "خردل"]),
    _i("champignons", "Champignons", "champignons", "فطر", "الفطر", False, False,
       ["champignon", "champignon de pari", "champi", "فطر"]),
    _i("cornichons", "Cornichons", "cornichons", "خيار مخلل", "الخيار المخلل", False, False,
       ["cornichon", "pickle", "خيار مخلل"]),
    _i("persil", "Persil", "persil", "بقدونس", "البقدونس", False, False,
       ["persil", "madnous", "ma3dnous", "معدنوس", "بقدونس"]),
    _i("capres", "Câpres", "câpres", "كبار", "الكبار", False, False, ["capre", "kabbar", "كبار"]),
    _i("chantilly", "Chantilly", "chantilly", "كريمة مخفوقة", "الكريمة المخفوقة", False, False,
       ["chantilly", "creme chantilly", "creme fouettee", "شانتيي", "كريمه مخفوقه"]),
    _i("ail", "Ail", "ail", "ثوم", "الثوم", True, True, ["ail", "gousse d ail", "ail frai", "thoum",
        "ثوم"], ["sauce"]),
    _i("poivron", "Poivron", "poivron", "فلفل حلو", "الفلفل الحلو", False, False, ["poivron", "فلفل حلو"]),
    _i("concombre", "Concombre", "concombre", "خيار", "الخيار", False, False, ["concombre", "خيار"],
        ["مخلل"]),
    _i("mais", "Maïs", "maïs", "ذرة", "الذرة", False, False, ["mais doux", "grain de mais", "ذره"]),
    _i("coriandre", "Coriandre", "coriandre", "كزبرة", "الكزبرة", False, False,
       ["coriandre", "kosbor", "كسبر", "قزبر", "كزبره"]),
    _i("cumin", "Cumin", "cumin", "كمون", "الكمون", False, False, ["cumin", "kamoun", "كمون"]),
    _i("basilic", "Basilic", "basilic", "ريحان", "الريحان", False, False, ["basilic", "basilic frai",
        "ريحان", "حبق"]),
    _i("roquette", "Roquette", "roquette", "جرجير", "الجرجير", False, False, ["roquette", "jarjir", "جرجير"]),
    _i("lait", "Lait", "lait", "حليب", "الحليب", False, False,
       ["lait", "lait entier", "lait demi ecreme", "lait ecreme", "حليب"], ["concentre", "poudre", "coco"]),
    _i("cacao", "Cacao", "cacao", "كاكاو", "الكاكاو", False, False, ["cacao", "cacao en poudre", "كاكاو"]),
    _i("menthe", "Menthe", "menthe", "نعناع", "النعناع", False, False,
       ["menthe", "menthe fraiche", "nana", "na3na3", "نعناع"]),
    _i("miel", "Miel", "miel", "عسل", "العسل", True, True, ["miel", "3sel", "عسل"]),
)
# fmt: on
BY_KEY = {i.key: i for i in LEXICON}
RANK = {i.key: n for n, i in enumerate(LEXICON)}
# raw spellings checked before accents are dropped (« maïs » is « mais » = « but » once folded)
RAW_ALIASES = {"mais": ("maïs",)}
_ALIASES = {i.key: tuple(norm(a).strip() for a in i.aliases if norm(a).strip()) for i in LEXICON}
_BLOCKERS = {i.key: tuple(norm(b).strip() for b in i.blockers) for i in LEXICON}
SPICY = ("piquant", "pimente", "spicy", "harr", "حار")


_SEGMENTS = re.compile(r"[,;/+&·()\n،]|\s(?:et|avec|and|w)\s", re.IGNORECASE)


@lru_cache(maxsize=8192)
def _segment_keys(raw: str) -> tuple[str, ...]:
    folded = norm(raw)
    low = raw.lower()
    found = []
    for item in LEXICON:
        key = item.key
        if any(has(folded, b) for b in _BLOCKERS[key] if b):
            hit = False  # a blocker in the same piece: « double concentré de tomate », « huile d'olive »
        else:
            hit = any(
                has(folded, a) and not has(folded, f"san {a}") and not has(folded, f"sans {a}")
                and not has(folded, f"بدون {a}")
                for a in _ALIASES[key]
            )  # fmt: skip
        if not hit and key in RAW_ALIASES:
            hit = any(a in low for a in RAW_ALIASES[key])
        if hit:
            found.append(key)
    return tuple(found)


def find_keys(text: Any) -> list[str]:
    """The lexicon keys named in ``text`` (an ingredient, a name, a description), lexicon order, read
    piece by piece (« Sauce tomate, mozzarella, tomate fraîche » has a fresh tomato). A key preceded by
    « sans » / « بدون » is NOT counted (« pizza sans olives »)."""
    raw = "" if text is None else str(text)
    keys: set[str] = set()
    for piece in _SEGMENTS.split(raw):
        if piece and piece.strip():
            keys.update(_segment_keys(piece))
    return sorted(keys, key=lambda k: RANK[k])


# ─────────────────────────────── dish types ───────────────────────────────

SANDWICH_KEYS = frozenset({
    "harissa", "oignon", "mayonnaise", "frites", "salade", "tomate", "fromage", "olives", "thon", "oeuf",
    "piment", "sauce_blanche", "sauce", "ketchup", "moutarde", "champignons", "cornichons", "persil",
    "capres", "ail", "poivron", "concombre", "mais", "basilic", "roquette", "coriandre",
})  # fmt: skip
PLATE_KEYS = frozenset({
    "harissa", "piment", "oignon", "fromage", "olives", "persil", "capres", "coriandre", "oeuf", "thon",
    "cumin", "ail", "champignons", "poivron", "frites", "salade", "sauce", "mayonnaise", "ketchup",
})  # fmt: skip
SALAD_KEYS = frozenset({
    "oignon", "tomate", "olives", "thon", "oeuf", "fromage", "concombre", "mais", "poivron", "piment",
    "harissa", "capres", "persil", "basilic", "roquette", "sauce", "mayonnaise", "coriandre",
})  # fmt: skip


@dataclass(frozen=True)
class DishType:
    name: str
    words: tuple[str, ...]  # normalized phrases that make this type
    allowed: frozenset[str]
    template: tuple[str, ...] = ()  # used only without a recipe
    cold: bool = False  # « glaçons » is a service option, recipe or not


def _t(name, words, allowed, template=(), cold=False) -> DishType:
    return DishType(name, tuple(norm(w).strip() for w in words), frozenset(allowed), tuple(template), cold)


# First match wins: the industrial drinks, then the dishes, then the drinks made on the spot.
# fmt: off
TYPES: tuple[DishType, ...] = (
    _t("bottle", ["eau", "eau minerale", "eau gazeuse", "soda", "coca", "coca cola", "cola", "fanta",
        "sprite",
                  "pepsi", "boga", "schweppes", "7up", "canette", "bouteille", "red bull", "safia", "sabrine",
                  "marwa", "aqua", "delice", "yaourt", "danone", "biere", "gazouz", "ماء", "قازوز"], ()),
    _t("makloub", ["makloub", "mahjouba", "maklouba", "مقلوب"], SANDWICH_KEYS,
       ("harissa", "mayonnaise", "frites", "fromage", "salade", "oignon")),
    _t("libanais", ["libanais", "libanai", "لبناني"], SANDWICH_KEYS,
       ("harissa", "mayonnaise", "frites", "fromage", "salade")),
    _t("chapati", ["chapati", "chapatti", "chappati", "شباتي"], SANDWICH_KEYS,
       ("harissa", "mayonnaise", "fromage", "salade")),
    _t("tacos", ["tacos", "taco", "french taco", "تاكوس"], SANDWICH_KEYS, ("harissa", "mayonnaise",
        "oignon", "salade")),
    _t("burger", ["burger", "hamburger", "cheeseburger", "برقر", "برغر"], SANDWICH_KEYS,
       ("oignon", "cornichons", "tomate", "salade", "fromage", "mayonnaise")),
    _t("sandwich", ["sandwich", "sandwiche", "kaskrout", "casse croute", "baguette farcie", "panini", "wrap",
                    "tabouna", "mlewi", "mlawi", "chawarma", "shawarma", "kebab", "fricasse", "سندويتش",
                        "كسكروت",
                    "ملاوي", "شاورما"], SANDWICH_KEYS,
       ("harissa", "mayonnaise", "salade", "oignon")),
    _t("pizza", ["pizza", "calzone", "بيتزا"],
       {"olives", "oignon", "harissa", "piment", "champignons", "poivron", "thon", "oeuf", "basilic",
           "roquette",
        "mais", "ail", "persil"}, ("olives", "harissa", "oignon")),
    _t("crepe", ["crepe", "galette", "gaufre", "waffle", "كريب"],
       {"fromage", "champignons", "oignon", "olives", "harissa", "piment", "chantilly", "thon", "tomate",
        "sauce", "mayonnaise", "persil"}),
    _t("omelette", ["omelette", "oeuf brouille", "oeuf au plat", "brik", "ojja", "chakchouka", "عجه", "بريك",
                    "شكشوكه", "مرقاز"],
       {"fromage", "thon", "harissa", "persil", "capres", "oignon", "piment", "champignons", "olives",
        "poivron", "tomate", "salade", "frites"}, ("harissa", "piment")),
    _t("lablabi", ["lablabi", "leblebi", "لبلابي"], {"harissa", "oeuf", "thon", "capres", "olives",
        "cumin", "persil"},
       ("harissa", "oeuf", "thon", "capres", "cumin", "olives")),
    _t("plate", ["couscous", "kafteji", "mloukhia", "marka", "tajine", "makarouna", "pate", "pasta", "penne",
                 "spaghetti", "tagliatelle", "lasagne", "ravioli", "macaroni", "riz", "grillade",
                     "mixed grill",
                 "escalope", "cordon bleu", "steak", "poulet roti", "poisson", "kamounia", "assiette", "plat",
                 "كسكسي", "كفتاجي", "ملوخية", "طاجين", "مقرونة", "مقرونه"], PLATE_KEYS, ("harissa",
                     "piment")),
    _t("fruit_salad", ["salade de fruit", "salade fruit", "سلطه غلال"], {"miel", "chantilly", "sucre"}),
    _t("salad", ["salade", "سلطه"], SALAD_KEYS, ("oignon", "olives", "tomate")),
    _t("milkshake", ["milkshake", "milk shake", "ميلك شيك"], {"chantilly", "sucre"}, ("chantilly",)),
    _t("smoothie", ["smoothie", "سموذي"], {"sucre", "miel", "glacons"}, ("sucre",), cold=True),
    _t("iced", ["iced", "ice tea", "ice latte", "ice coffee", "frappuccino", "frappe", "cafe glace",
        "mocha glace",
                "latte glace", "cappuccino glace", "the glace", "قهوه مثلجه"],
       {"sucre", "chantilly", "glacons", "cacao"}, ("sucre",), cold=True),
    _t("lemonade", ["citronnade", "limonade", "lemonade", "citron presse", "jus de citron", "mojito",
                    "ليموناضة", "ليموناده"], {"sucre", "glacons", "menthe"}, ("sucre",), cold=True),
    _t("juice", ["jus", "juice", "cocktail de fruit", "detox", "عصير"], {"sucre", "glacons", "lait",
        "menthe", "miel"},
       ("sucre",), cold=True),
    _t("hot_chocolate", ["chocolat chaud", "chocolat au lait", "nutella au lait", "lait chaud", "chocolat"],
       {"sucre", "chantilly"}),
    _t("coffee", ["cafe", "coffee", "espresso", "expresso", "express", "ristretto", "americano", "macchiato",
                  "capucin", "cappuccino", "capuccino", "latte", "cortado", "flat white", "mocaccino",
                      "mocha",
                  "direct", "nescafe", "kahwa", "قهوه", "كابوسان", "نسكافيه"], {"sucre", "chantilly",
                      "cacao"}, ("sucre",)),
    _t("tea", ["the", "tea", "infusion", "verveine", "تاي", "شاي"], {"sucre", "menthe"}, ("sucre",)),
    _t("fried_sweet", ["bambalouni", "beignet", "ftayer", "makroudh", "makroud", "zlabia", "بمبالوني",
        "فطاير"],
       {"sucre", "miel"}, ("sucre",)),
    _t("dessert", ["tiramisu", "cheesecake", "fondant", "gateau", "tarte", "patisserie", "mille feuille",
        "cake",
                   "muffin", "cookie", "brownie", "croissant", "pain au chocolat", "viennoiserie", "glace",
                   "affogato", "dessert", "baklawa", "kaak", "حلويات"], {"chantilly"}),
)
# fmt: on
VOLUME = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:cl|ml|l|litre?s?)\b", re.IGNORECASE)
NO_OPTIONS = DishType("bottle", (), frozenset())
UNKNOWN = DishType(
    "unknown", (),
    frozenset({"harissa", "piment", "oignon", "mayonnaise", "ketchup", "olives", "champignons", "persil",
               "capres", "cornichons", "sauce_blanche", "sauce", "moutarde", "ail", "coriandre"}),
)  # fmt: skip


def dish_type(name: Any, category: Any = None, description: Any = None) -> DishType:
    """The dish's type from its name, then its category, then its description; a volume (« 1,5 L »,
    « 33 cl ») with nothing else known = a bottle."""
    for text in (name, category, description):
        folded = norm(text)
        if folded.strip():
            for t in TYPES:
                if t.name == "tea":
                    # « The Burger » is a burger (the dishes come first); a « thé » starts the name
                    if folded.startswith(" the ") or any(has(folded, w) for w in t.words if w != "the"):
                        return t
                elif any(has(folded, w) for w in t.words):
                    return t
    if VOLUME.search(str(name or "")):
        return NO_OPTIONS
    return UNKNOWN


# ─────────────────────────────── one dish ───────────────────────────────


def _custom_key(name: str) -> str:
    slug = norm(name).strip().replace(" ", "-")
    return ("o-" + slug)[:48] if slug else "o-x"


def _option(key: str, levels: tuple[str, ...]) -> dict[str, Any]:
    item = BY_KEY[key]
    labels = item.labels()
    return {
        "key": key,
        "name": {"fr": item.fr, "ar": item.ar},
        "levels": list(levels),
        "labels": {lv: labels[lv] for lv in levels},
    }


def custom_option(key: str, name_fr: str, name_ar: str | None, levels: tuple[str, ...]) -> dict[str, Any]:
    """An option the restaurant (ODS « always ») or the admin added: generic labels."""
    noun = name_fr[:1].lower() + name_fr[1:] if name_fr[:2] != name_fr[:2].upper() else name_fr
    ar = name_ar or name_fr
    labels = {"none": {"fr": f"Sans {noun}", "ar": f"بدون {ar}"}}
    if "less" in levels:
        elided = noun[:1].lower() in "aeiouyéèêâîôœ"
        labels["less"] = {"fr": f"Peu d'{noun}" if elided else f"Peu de {noun}", "ar": f"كمية قليلة من {ar}"}
    return {"key": key, "name": {"fr": name_fr, "ar": ar}, "levels": list(levels), "labels": labels}


# The main thing of a dish when the lexicon does not know it (« Crêpe POULET champignons gruyère »):
# what comes after it in the title can be removed, it cannot.
# fmt: off
MAIN_WORDS = tuple(norm(w).strip() for w in (
    "poulet", "chicken", "jambon", "viande", "boeuf", "escalope", "merguez", "kefta", "kofta", "saumon",
    "crevette", "poisson", "dinde", "chawarma", "shawarma", "pepperoni", "salami", "foie", "agneau", "veau",
    "kebab", "nugget", "cordon bleu", "steak", "bolognaise", "carbonara", "fruits de mer", "calamar",
    "nutella", "lotus", "speculoos", "oreo", "banane", "fraise", "chocolat", "caramel", "vanille", "pistache",
    "amande", "pignon", "datte", "mangue", "ananas", "kiwi", "peche", "pomme", "orange", "citron",
        "framboise",
    "epinard", "dejej", "kafteji", "دجاج", "لحم", "تن",
))
# fmt: on
# Dishes defined by several ingredients at once (a « caprese » without tomato is not a caprese).
SIGNATURES = {
    norm(k).strip(): frozenset(v)
    for k, v in {
        "caprese": {"fromage", "tomate", "basilic"},
        "margherita": {"fromage", "tomate", "basilic"},
        "margarita": {"fromage", "tomate", "basilic"},
        "mechouia": {"piment", "tomate", "poivron", "ail"},
        "chakchouka": {"poivron", "tomate", "oeuf", "piment"},
        "ojja": {"oeuf", "tomate", "piment", "harissa"},
        "kafteji": {"oeuf", "poivron", "piment"},
        "cesar": {"salade", "fromage"},
        "caesar": {"salade", "fromage"},
        "nicoise": {"thon", "oeuf", "olives", "tomate"},
        "tunisienne": {"tomate", "oignon", "concombre", "piment"},
        "lablabi": {"cumin"},
    }.items()
}
CHOICE_WORDS = (" ou ", " au choix ", " او ")
# the word that names the dish type is not one of its ingredients (« SALADE burrata »)
TYPE_WORD_KEYS = {"salad": "salade"}


def _first_at(folded: str, phrases: tuple[str, ...]) -> int:
    hits = [folded.find(f" {p} ") for p in phrases if p]
    hits = [h for h in hits if h >= 0]
    return min(hits) if hits else -1


def title_rules(name: Any, kind: DishType) -> tuple[set[str], bool]:
    """(the keys the title makes part of the dish, spicy). In the title, the FIRST thing named is the
    dish (« Sandwich THON, œufs, mayonnaise »): the ingredients named after it stay removable; a choice
    (« thon ou jambon ») or a signature dish (caprese, ojja…) protects all of them."""
    folded = norm(name)
    spicy = any(has(folded, s) for s in SPICY)
    own = TYPE_WORD_KEYS.get(kind.name)
    named = {k: _first_at(folded, _ALIASES[k]) for k in find_keys(name)}
    protected = {own} if own and own in named else set()
    named = {k: at for k, at in named.items() if at >= 0 and k != own}
    for word, keys in SIGNATURES.items():
        if has(folded, word):
            protected |= keys
    if named:
        if any(w in folded for w in CHOICE_WORDS):
            protected |= set(named)
        else:
            main = _first_at(folded, MAIN_WORDS)
            head = min(named.values())
            if main < 0 or head < main:
                protected |= {k for k, at in named.items() if at == head}
    if spicy:
        protected |= {"harissa", "piment"}
    return protected, spicy


def candidates(
    name: Any,
    description: Any = None,
    category: Any = None,
    ingredients: list[dict[str, Any]] | None = None,
    shop_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Every removal this dish allows (uncapped, lexicon order): what a customer may send."""
    if shop_kind not in FOOD_SHOPS:
        return []
    kind = dish_type(name, category, description)
    if not kind.allowed:
        return []
    protected, spicy = title_rules(name, kind)
    never: set[str] = set()
    always: list[tuple[str, str]] = []  # (key, ODS name) of « removable: true » outside the lexicon
    forced: set[str] = set()  # « removable: true » on a lexicon ingredient: the restaurant said so
    found: list[str] = []
    recipe = [i for i in (ingredients or []) if isinstance(i, dict) and i.get("name")]
    for ing in recipe:
        keys = find_keys(ing["name"])
        if ing.get("removable") is False:
            never.update(keys)
            continue
        if ing.get("removable") is True:
            forced.update(keys)
            if not keys:
                always.append((_custom_key(str(ing["name"])), str(ing["name"])[:INGREDIENT_NAME_MAX]))
        found.extend(k for k in keys if k not in found)
    described = find_keys(description)
    for k in described:
        if k not in found:
            found.append(k)
    if not recipe:
        # a description that lists the dish (2 ingredients or more) is its recipe: no template
        template = kind.template if len(described) < 2 else ()
        for k in (*find_keys(name), *template):
            if k not in found:
                found.append(k)
    if kind.cold and "glacons" not in found:
        found.append("glacons")  # ice is service, never in a recipe
    if spicy and not ({"harissa", "piment"} & set(found)):
        found.append("piment")
    out: list[dict[str, Any]] = []
    for key in sorted(dict.fromkeys(found), key=lambda k: RANK[k]):
        if key in never or (key not in kind.allowed and key not in forced):
            continue
        item = BY_KEY[key]
        levels: tuple[str, ...] = ("less", "none") if item.less else ("none",)
        if key in protected and key not in forced:
            if not (item.condiment and item.less):
                continue  # « Sandwich thon » without tuna is another dish
            levels = ("less",)
        out.append(_option(key, levels))
    seen = {o["key"] for o in out}
    for key, label in always:
        if key not in seen:
            seen.add(key)
            out.append(custom_option(key, label, None, ("none",)))
    return out


def apply_override(auto: list[dict[str, Any]], override: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The admin's corrections over the engine: hidden keys out, added ones first."""
    if not isinstance(override, dict):
        return list(auto)
    hidden = set(override.get("hidden") or [])
    added = [
        custom_option(a["key"], a["name_fr"], a.get("name_ar"), tuple(a.get("levels") or ("none",)))
        for a in override.get("added") or []
        if isinstance(a, dict) and a.get("key") and a.get("name_fr")
    ]
    keys = {a["key"] for a in added}
    return [*added, *(o for o in auto if o["key"] not in hidden and o["key"] not in keys)]


def rank(
    options: list[dict[str, Any]],
    product_counts: dict[str, int] | None = None,
    category_counts: dict[str, int] | None = None,
    order: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Most removed on this dish, then on its category, then the given order; at most 6."""
    pc, cc = product_counts or {}, category_counts or {}
    pinned = {k: n for n, k in enumerate(order or [])}
    position = {o["key"]: n for n, o in enumerate(options)}
    ranked = sorted(
        options,
        key=lambda o: (
            pinned.get(o["key"], len(pinned)),
            -pc.get(o["key"], 0),
            -cc.get(o["key"], 0),
            position[o["key"]],
        ),
    )
    return ranked[:MAX_SHOWN]


def for_product(product: Any, category_name: str | None, shop_kind: str | None) -> list[dict[str, Any]]:
    """Every option a customer may send for this product (engine + admin corrections, uncapped)."""
    auto = candidates(product.name, product.description, category_name, product.ods_ingredients, shop_kind)
    return apply_override(auto, product.options_override)


# ─────────────────────────────── the shop ───────────────────────────────


async def stats(session: AsyncSession, shop_id: uuid.UUID) -> dict[uuid.UUID, dict[str, int]]:
    from app.models import VitrineOptionStat

    out: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    for row in (
        await session.execute(
            select(VitrineOptionStat.product_id, VitrineOptionStat.key, VitrineOptionStat.count).where(
                VitrineOptionStat.shop_id == shop_id
            )
        )
    ).all():
        out[row.product_id][row.key] = int(row.count)
    return out


async def shop_options(
    session: AsyncSession, shop: Any, products: list[Any], categories: list[Any]
) -> dict[uuid.UUID, dict[str, Any]]:
    """{product id: {"remove": [≤ 6 ranked options]}} for a whole Vitrine (one stats query)."""
    kind = shop.categories[0] if getattr(shop, "categories", None) else None
    names = {c.id: c.name for c in categories}
    counts = await stats(session, shop.id)
    by_category: dict[Any, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in products:
        for key, n in counts.get(p.id, {}).items():
            by_category[p.category_id][key] += n
    out = {}
    for p in products:
        options = for_product(p, names.get(p.category_id), kind)
        order = (p.options_override or {}).get("order") if isinstance(p.options_override, dict) else None
        out[p.id] = {
            "remove": rank(
                options, counts.get(p.id), by_category.get(p.category_id) if p.category_id else None, order
            )
        }
    return out


# ─────────────────────────────── the order ───────────────────────────────


def parse_mods(raw: Any) -> list[dict[str, str]] | None:
    """The `mods` of a cart line, shape only (400 invalid_cart on a bad shape): [{key, level}]."""
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_MODS:
        return None
    out, seen = [], set()
    for m in raw:
        if not isinstance(m, dict):
            return None
        key, level = m.get("key"), m.get("level")
        if not isinstance(key, str) or not 1 <= len(key) <= 48 or level not in LEVELS or key in seen:
            return None
        seen.add(key)
        out.append({"key": key, "level": level})
    return out


def signature(mods: list[dict[str, str]]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((m["key"], m["level"]) for m in mods))


def resolve(
    mods: list[dict[str, str]], options: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The mods against the product's options → (cart mods with their labels, unknown keys)."""
    by_key = {o["key"]: o for o in options}
    known, bad = [], []
    for m in mods:
        option = by_key.get(m["key"])
        if option is None or m["level"] not in option["levels"]:
            bad.append(m["key"])
            continue
        label = option["labels"][m["level"]]
        known.append(
            {"key": m["key"], "level": m["level"], "label_fr": label["fr"], "label_ar": label["ar"],
             "price_delta": "0.000"}
        )  # fmt: skip
    order = {o["key"]: n for n, o in enumerate(options)}
    known.sort(key=lambda m: order[m["key"]])
    return known, bad


def mods_text(mods: list[dict[str, Any]]) -> str:
    return " · ".join(m["label_fr"] for m in mods)


async def record(session: AsyncSession, shop_id: uuid.UUID, lines: list[dict[str, Any]]) -> None:
    """+1 per ordered line for each removal (aggregated, no customer): the order of the chips. One
    statement, rows in a fixed order (two orders at once never lock each other), in a savepoint: a
    failure here never costs the order."""
    from app.models import VitrineOptionStat

    counts: dict[tuple[str, str], int] = defaultdict(int)
    for line in lines:
        for m in line.get("mods") or []:
            counts[(line["product_id"], m["key"])] += 1
    if not counts:
        return
    rows = [
        {"shop_id": shop_id, "product_id": uuid.UUID(product_id), "key": key, "count": n}
        for (product_id, key), n in sorted(counts.items())
    ]
    stmt = insert(VitrineOptionStat).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["product_id", "key"], set_={"count": VitrineOptionStat.count + stmt.excluded.count}
    )
    try:
        async with session.begin_nested():
            await session.execute(stmt)
    except Exception:
        logger.warning("vitrine options: stats not recorded for shop %s", shop_id)


# ─────────────────────────────── the admin ───────────────────────────────


def clean_override(payload: dict[str, Any], auto_keys: set[str]) -> dict[str, Any] | None:
    """setVitrineProductOptions body → the stored override (None = back to automatic)."""
    if payload.get("reset") is True:
        return None
    hidden = payload.get("hidden") or []
    added_raw = payload.get("added") or []
    order = payload.get("order") or []
    if not isinstance(hidden, list) or not isinstance(added_raw, list) or not isinstance(order, list):
        raise ApiError(400, "validation_error", "hidden, added and order are lists")
    if len(added_raw) > MAX_ADDED:
        raise ApiError(400, "validation_error", f"added: at most {MAX_ADDED}")
    added, keys = [], set()
    for a in added_raw:
        if not isinstance(a, dict):
            raise ApiError(400, "validation_error", "added: objects")
        name_fr = " ".join(str(a.get("name_fr") or "").split())[:CUSTOM_NAME_MAX]
        name_ar = " ".join(str(a.get("name_ar") or "").split())[:CUSTOM_NAME_MAX] or None
        levels = a.get("levels") or ["none"]
        if len(name_fr) < 1 or levels not in (["none"], ["less", "none"]):
            raise ApiError(400, "validation_error", "added: name_fr 1–40, levels [none] or [less, none]")
        key = "c-" + norm(name_fr).strip().replace(" ", "-")[:40]
        if key in keys:
            continue
        keys.add(key)
        added.append({"key": key, "name_fr": name_fr, "name_ar": name_ar, "levels": levels})
    valid = auto_keys | keys
    hidden = sorted({h for h in hidden if isinstance(h, str) and h in auto_keys})
    order = [k for k in dict.fromkeys(o for o in order if isinstance(o, str)) if k in valid][:MAX_SHOWN]
    if not hidden and not added and not order:
        return None
    return {"hidden": hidden, "added": added, "order": order}
