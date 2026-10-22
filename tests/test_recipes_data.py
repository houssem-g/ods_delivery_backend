"""The recipe files themselves (QA 22/10, owner n° 22, 23, 26, 18): standard Arabic, halal texts without pork
or wine, one « Poivron vert », the photo fix file readable. Pure: reads data/recipes, no database."""

import json
import re
from pathlib import Path

from app.services import recipe_photos, recipes_import

DATA = Path(__file__).resolve().parent.parent / "data" / "recipes"
LETTER = "\u0621-\u0652"
# Tunisian words replaced by their standard Arabic (the local name of a dish may keep them: `nom_local`)
DARIJA = (
    "معدنوس", "قصبر", "أكحل", "سفنارية", "قارص", "فارينة", "جلبانة", "جلجلان", "تابل", "كروية", "رند", "علوش",
    "ملسوقة", "قمبري", "ياغورت", "موتارد", "قرويار", "تن", "روز", "ليم", "فقوس", "كرافس", "قرنيط",
)  # fmt: skip
DARIJA_RE = re.compile(rf"(?<![{LETTER}])(?:وال|بال|فال|لل|ال|و|ب|ل|ف)?(?:{'|'.join(DARIJA)})(?![{LETTER}])")
NOT_HALAL_FR = re.compile(r"porc|\bvins?\b|bière|alcool|rhum|kirsch|calvados|lardons", re.I)
NOT_HALAL_AR = re.compile(rf"(?<![{LETTER}])(?:ال|ب|و)?(?:خنزير|خمر|كحول|جعة|مقدّد)(?![{LETTER}])")


def _recipes():
    for path in sorted(DATA.glob("recettes*.json")):
        yield from recipes_import.recipes_of(json.loads(path.read_text(encoding="utf-8")))


def _texts(r, lang):
    yield r.get(f"nom_{lang}") or ""
    yield r.get(f"astuce_{lang}") or ""
    yield from r.get(f"etapes_{lang}") or []
    for line in r["ingredients"]:
        yield line.get(f"nom_{lang}") or ""
        yield line.get(f"note_{lang}") or ""


def test_arabic_names_and_steps_are_standard_arabic():
    dictionary = json.loads((DATA / "ingredients_tn.json").read_text(encoding="utf-8"))
    assert not [i["ar"] for i in dictionary if DARIJA_RE.search(i["ar"]) or "/" in i["ar"]]
    eggs = next(i for i in dictionary if i["key"] == "oeufs")
    assert eggs["ar"] == "بيض" and "عظم" in eggs["darija"]  # « عظم » is « os »: kept only to be found
    bad = [(r["id"], t) for r in _recipes() for t in _texts(r, "ar") if DARIJA_RE.search(t)]
    assert bad == []


def test_no_pork_wine_or_alcohol_mentioned():
    bad = [(r["id"], t) for r in _recipes() for t in _texts(r, "fr") if NOT_HALAL_FR.search(t)]
    bad += [(r["id"], t) for r in _recipes() for t in _texts(r, "ar") if NOT_HALAL_AR.search(t)]
    assert bad == []


def test_one_green_pepper_name_and_the_fixed_lines():
    lines = [(r["id"], line) for r in _recipes() for line in r["ingredients"]]
    assert {line["ingredient_id"] for _, line in lines if line.get("nom_fr") == "Poivron vert"} == {5}
    assert {line["nom_fr"] for _, line in lines if line.get("ingredient_id") == 153} == {"Piment doux long"}
    mint = next(line for slug, line in lines if slug == "silq-mahshi" and line["ingredient_id"] == 19)
    assert (mint["quantite"], mint["unite"]) == (0.5, "botte")
    juice = next(line for slug, line in lines if slug == "baeckeoffe" and line["ingredient_id"] == 172)
    assert juice["nom_fr"] == "Jus de raisin blanc" and "note_fr" not in juice


def test_photo_fix_file_removes_two_photos_and_credits_the_brioche():
    fix = json.loads((DATA / "photos_fix_2210.json").read_text(encoding="utf-8"))
    for slug in ("djaj-mhammer", "moules-marinieres"):
        cols, reason = recipe_photos.parse_entry(slug, fix[slug])
        assert reason is None and cols["photo_key"] is None
    cols, reason = recipe_photos.parse_entry("brioche", fix["brioche"])
    assert reason is None and cols["photo_key"] == "public/recipes/brioche.webp"
    assert cols["photo_credit"]["author"] == "Rainer Zenz" and cols["photo_credit"]["credit_required"] is True


# Owner rule 22/10: the app speaks to a Muslim culture — no recipe holds pork or alcohol, and « halal » is never
# said (everything is). Any text a recipe or an ingredient can show (titles, notes, steps, tips, lines, the
# dictionary and its search words), in French and Arabic; only URLs and ids (the address) are left out.
FORBIDDEN_FR = re.compile(r"porc|lardon|\bvins?\b|bière|alcool|rhum|cidre|\bhalal\b", re.I)
FORBIDDEN_AR = re.compile(rf"(?<![{LETTER}])(?:ال|ب|و|ل)?(?:خنزير|خمر|نبيذ|كحول|حلال)(?![{LETTER}])")
NOT_SHOWN = {"id", "slug", "url", "urls", "source", "sources", "lien", "liens"}


def _strings(node, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in NOT_SHOWN:
                yield from _strings(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, key)
    elif isinstance(node, str) and not node.startswith(("http://", "https://")):
        yield node


def test_no_pork_alcohol_nor_halal_shown_anywhere():
    files = [*sorted(DATA.glob("recettes*.json")), DATA / "ingredients_tn.json"]
    files.append(Path(recipes_import.__file__).with_name("recipes_pantry_words.json"))
    bad = [
        (path.name, text)
        for path in files
        for text in _strings(json.loads(path.read_text(encoding="utf-8")))
        if FORBIDDEN_FR.search(text) or FORBIDDEN_AR.search(text)
    ]
    assert bad == []
