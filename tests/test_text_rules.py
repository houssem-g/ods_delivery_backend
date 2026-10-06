"""Static check of the texts the server sends to people (QA 06/10, wave 3): notifications, push,
e-mails, chat lines and the error messages shown in the app. Reads app/**/*.py with `ast` (no
database, no network) and fails when an owner rule is broken:

- tutoiement: French « vous » everywhere, couriers included (B79);
- tnd: « DT » in French, « د.ت » in Arabic, never « TND » on screen (B78);
- latin-in-ar: no Latin town name, « DT », « km », « min » inside an Arabic text (B75);
- ar-colon: no French-style space before « : » in an Arabic text (B80);
- ar-agreement: known wrong agreements, « % » instead of « ٪ » (B76);
- km-point: a distance « 5.0 km » in French takes a decimal comma (B79).

Developer-only strings are skipped: docstrings, log calls, SQL, codes without spaces, comments.
EXCEPTIONS lists the few justified ones (file, exact text, reason).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

AR = re.compile(r"[؀-ۿ]")
TU_VERBS = (
    "Va|Prends|Dépense|Fais|Choisis|Vérifie|Appelle|Ouvre|Touche|Écris|Envoie|Regarde|Attends|Mets|Ajoute|"
    "Glisse|Photographie|Saisis|Indique|Rends|Revends|Confirme|Annule|Clique|Remplis|Sélectionne|Essaie|"
    "Réessaie|Contacte|Sois|Pense|Dis|Reste|Préviens|Accepte|Refuse|Appuie|Utilise|Lis|Donne|Prépare|Laisse|"
    "Viens|Descends|Achète|Paie|Montre|Retourne|Patiente|Profite"
)
TU_VERB = re.compile(
    rf"(?:^|[.!?:«—]\s+|\n\s*)(?:{TU_VERBS})(?=[\s,!.]|$)"
    rf"|(?:^|[.!?:«—]\s+)(?:Ne |N')\s*(?:{TU_VERBS.lower()})\b"
)
TU_PRONOUN = re.compile(
    r"(?:^|[\s«(])(?:tu|toi|ton|ta|tes|te|Tu|Toi|Ton|Ta|Tes)(?=[\s,.!?»)]|$)|(?:^|[\s«(])[tT]'(?=[a-zéèêàâîôûh])"
)
TOWNS = (
    "Sousse|Sahloul|Khezama|Khézama|Tunis|Sfax|Monastir|Mahdia|Nabeul|Hammamet|Kairouan|Bizerte|Ariana|Suisse"
)
LATIN_IN_AR = re.compile(rf"\b(?:{TOWNS}|DT|TND)\b|\d\s*(?:km|min)\b", re.IGNORECASE)
AR_BAD = ("مندوبان متصل ", "مندوبان نشط ", "متصلون حول")
AR_PERCENT = re.compile(r"\d\s*%|%\s*\d")
KM_POINT = re.compile(r"\d\.\d\s*km\b")

# (file relative to app/, exact text fragment, why it is allowed)
EXCEPTIONS: list[tuple[str, str, str]] = []

SKIP_CALLS = {
    "debug",
    "info",
    "warning",
    "error",
    "exception",
    "critical",  # log.*
    "text",
    "select",
    "execute",
    "where",
    "label",
    "Column",
    "mapped_column",
    "CheckConstraint",
    "getenv",
    "get",
    "getattr",
    "setdefault",
    "startswith",
    "endswith",
    "split",
    "replace",
    "join",
    "match",
    "search",
    "compile",
    "sub",
    "fullmatch",
    "strftime",
    "strptime",
    "format_exc",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _texts(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    skip: set[int] = set()
    for node in ast.walk(tree):
        # docstrings
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
        if isinstance(node, ast.Call) and _call_name(node.func) in SKIP_CALLS:
            for sub in ast.walk(node):
                skip.add(id(sub))
        if isinstance(node, ast.Compare):  # comparisons with codes
            for sub in ast.walk(node):
                skip.add(id(sub))
        if isinstance(node, ast.Dict):  # keys are codes
            for key in node.keys:
                if key is not None:
                    for sub in ast.walk(key):
                        skip.add(id(sub))
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.JoinedStr):
            parts = [
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{…}"
                for v in node.values
            ]
            out.append((node.lineno, "".join(parts)))
            for v in node.values:
                skip.add(id(v))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
    return out


def _human(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if AR.search(t):
        return True
    has_words = bool(re.search(r"\s", t)) and bool(re.search(r"[A-Za-zÀ-ÿ]{2,}", t))
    return has_words and not re.fullmatch(r"[\w./:@?&=#%{}…-]+", t)


def find_problems() -> list[str]:
    problems: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith("migrations/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, raw in _texts(tree):
            t = " ".join(raw.split())
            if not _human(t) or any(f == rel and frag in t for f, frag, _ in EXCEPTIONS):
                continue

            def add(rule: str, why: str, *, _rel: str = rel, _line: int = line, _t: str = t) -> None:
                problems.append(f"{_rel}:{_line} [{rule}] {_t[:120]} — {why}")

            if re.search(r"\bTND\b", t):
                add("tnd", "« DT » en français, « د.ت » en arabe")
            if AR.search(t):
                latin = LATIN_IN_AR.search(t)
                if latin:
                    add("latin-in-ar", f"« {latin.group(0)} » en lettres latines dans un texte arabe")
                if re.search(r"[؀-ۿ)»]\s+:", t):
                    add("ar-colon", "espace avant « : » dans un texte arabe")
                for bad in AR_BAD:
                    if bad.strip() in t and (not bad.endswith(" ") or bad in t or t.endswith(bad.strip())):
                        add("ar-agreement", f"accord faux : « {bad.strip()} »")
                if AR_PERCENT.search(t):
                    add("ar-agreement", "pourcentage : « 83٪ » en arabe")
            else:
                if TU_PRONOUN.search(t):
                    add("tutoiement", "pronom de la 2e personne du singulier")
                if TU_VERB.search(t):
                    add("tutoiement", "impératif tutoyé")
                if KM_POINT.search(t):
                    add("km-point", "« 5,0 km » : virgule décimale en français")
    return problems


def test_server_texts_follow_the_owner_rules() -> None:
    problems = find_problems()
    assert not problems, "\n".join(problems)


def test_the_checker_catches_each_rule(tmp_path) -> None:
    """The rules really bite (a checker that never fails proves nothing)."""
    bad = tmp_path / "app"
    bad.mkdir()
    (bad / "bad_texts.py").write_text(
        'A = "Prends le ticket en photo"\n'
        'B = "Total : 12.000 TND"\n'
        'C = "المندوب في Sousse الآن"\n'
        'D = "آخر تحديث : اليوم"\n'
        'E = "مندوبان متصل حول المتجر"\n'
        'F = f"À {1} - 5.0 km"\n'
        'G = "Ton gain du jour"\n',
        encoding="utf-8",
    )
    globals()["APP"] = bad  # find_problems reads the module-level APP
    try:
        rules = {p.split("[", 1)[1].split("]", 1)[0] for p in find_problems()}
    finally:
        globals()["APP"] = Path(__file__).resolve().parents[1] / "app"
    assert rules == {"tutoiement", "tnd", "latin-in-ar", "ar-colon", "ar-agreement", "km-point"}


def test_new_order_distance_reads_like_the_app() -> None:
    """B79: « À 5,3 km » in French (the notification said « 5.3 km »), « 5.3 كم » in Arabic."""
    from app.services import order_texts

    text = order_texts.new_order("Pain", "Monoprix", 5.26)
    assert text["body_fr"].endswith("À 5,3 km") and text["body_ar"].endswith("5.3 كم")
    pref = order_texts.new_order_preferred("Sami", "Pain", "Monoprix", 1.04)
    assert pref["body_fr"].endswith("À 1,0 km") and pref["body_ar"].endswith("1.0 كم")
