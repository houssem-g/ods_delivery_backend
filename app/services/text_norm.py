"""Text normalization of the place search.

One tokenizer for every search path (searchPlaces, searchByBbox, geocodeAddress) and for
the precomputed `places.name_norm` / `places.search_norm` columns, so a query token and
the stored text are always normalized the same way:

    lower case → NFD → combining accents (U+0300–U+036F) removed → split on anything
    that is not a Unicode letter or digit (JS `/[^\\p{L}\\p{N}]+/u`).

Arabic stays searchable (no ASCII-only `\\w`, which would empty Arabic names);
Arabic short vowels (harakat, U+064B–U+065F, U+0670) and the tatweel (U+0640) are also
removed so a vocalized spelling matches a bare one.

Latin ↔ Arabic (QA 08/10, B3): a shop is written « Monoprix » on one map and « مونوبري » on
another, « Carrefour » / « كارفور ». Every token also gets a sound skeleton in one shared
alphabet (consonants only, vowels and h dropped, p→b, v→f, ch/ش→S...): « monoprix » and
« مونوبري » both give « mnbr ». `search_norm` keeps these skeletons after the words as
« ~mnbr », and a query token also matches a stored skeleton that starts with its own
(3+ letters) or equals it (2 letters, from a word of 5+ letters).
"""

import re
import unicodedata

_ACCENTS = re.compile(r"[\u0300-\u036f\u064b-\u065f\u0670\u0640]")


def _fold(value: object) -> str:
    text = "" if value is None else str(value)
    return _ACCENTS.sub("", unicodedata.normalize("NFD", text.lower()))


def _is_word_char(char: str) -> bool:
    return unicodedata.category(char)[0] in ("L", "N")


def tokenize(*values: object) -> list[str]:
    """Letters/digits runs of the folded text of every value (`tokenize`)."""
    tokens: list[str] = []
    for value in values:
        current: list[str] = []
        for char in _fold(value):
            if _is_word_char(char):
                current.append(char)
            elif current:
                tokens.append("".join(current))
                current = []
        if current:
            tokens.append("".join(current))
    return tokens


def normalize_text(*values: object) -> str:
    """Tokens joined by one space (searchPlaces `normalizeText`)."""
    return " ".join(tokenize(*values))


def compact(value: object) -> str:
    """Folded letters and digits only, no separator (proposeShop `norm`, duplicate check)."""
    return "".join(tokenize(value))


def fold_trim(value: object) -> str:
    """Folded and trimmed, punctuation kept (searchByBbox `normalizeText`)."""
    return _fold(value).strip()


def search_text(name: object, address: object, city: object) -> str:
    """`places.search_norm`: the normalized name, address and city of a place.

    A query token (letters/digits only) is a substring of a haystack token exactly when
    it is a substring of this space-joined string, so `LIKE '%token%'` computes
    `haystack.some(h => h.includes(token))` in SQL. The category, the other part of the
    haystacks, is matched on the (small) category vocabulary instead.
    """
    words = normalize_text(name, address, city)
    return " ".join([words, *skeletons(name, address, city)]) if words else words


# --- Latin ↔ Arabic sound skeleton (B3) ----------------------------------------------------

SKELETON_MARK = "~"
MIN_SKELETON = 3  # a shorter query skeleton must be a whole stored one, and come from a long word
SHORT_SKELETON_WORD = 5  # « aziza » → « zz », « géant » → « jn »: kept; « pain » → « bn »: not

_AR_TO_SKELETON = {
    "ب": "b", "پ": "b", "ت": "t", "ث": "t", "ط": "t", "ج": "j", "خ": "k", "د": "d", "ذ": "d",
    "ض": "d", "ظ": "d", "ر": "r", "ز": "z", "س": "s", "ص": "s", "ش": "S", "غ": "g", "ڤ": "g",
    "گ": "g", "ف": "f", "ق": "k", "ك": "k", "ل": "l", "م": "m", "ن": "n",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4", "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
}  # fmt: skip
# vowels, alif / hamza forms, ta marbuta, ain, ha and h: no consonant of their own
_AR_SILENT = set("اأإآءؤئىيوةعهح")
_LATIN_DIGRAPHS = (("sch", "S"), ("ch", "S"), ("sh", "S"), ("ph", "f"), ("kh", "k"), ("gh", "g"),
                   ("th", "t"), ("ck", "k"), ("qu", "k"), ("dj", "j"))  # fmt: skip
_LATIN_SILENT = set("aeiouyhw")


def _is_arabic(char: str) -> bool:
    return "\u0600" <= char <= "\u06ff"


def _dedupe(chars: list[str]) -> str:
    """Joins the consonants; a doubled one (« rr ») counts once, "" (a vowel) separates."""
    out: list[str] = []
    prev = ""
    for c in chars:
        if c and c != prev:
            out.append(c)
        prev = c
    return "".join(out)


def _arabic_skeleton(token: str) -> str:
    if token.startswith("ال") and len(token) > 4:
        token = token[2:]  # the article: « الجيان » is « Géant »
    return _dedupe(["" if c in _AR_SILENT else _AR_TO_SKELETON.get(c, "") for c in token])


def _latin_skeleton(token: str) -> str:
    if len(token) > 4 and token[-1] in "xstd" and token[-2] not in "xstd":
        token = token[:-1]  # silent French ending: monopri(x), gean(t)
    for digraph, sound in _LATIN_DIGRAPHS:
        token = token.replace(digraph, sound)
    out: list[str] = []
    for i, c in enumerate(token):
        nxt = token[i + 1] if i + 1 < len(token) else ""
        prv = token[i - 1] if i else ""
        if c in _LATIN_SILENT:
            out.append("")
        elif c == "s" and prv in _LATIN_SILENT and prv and nxt in _LATIN_SILENT and nxt:
            out.append("z")  # « magasin » sounds « magazin »
        elif c == "c":
            out.append("s" if nxt in ("e", "i", "y") else "k")
        elif c == "g":
            out.append("j" if nxt in ("e", "i", "y") else "g")
        elif c == "x":
            out.extend(("k", "", "s"))
        else:
            out.append({"q": "k", "p": "b", "v": "f"}.get(c, c))
    return _dedupe(out)


def skeleton(token: str) -> str:
    """The sound skeleton of one normalized token (« monoprix » / « مونوبري » → « mnbr »)."""
    if any(_is_arabic(c) for c in token):
        return _arabic_skeleton(token)
    return _latin_skeleton(token)


def query_skeleton(token: str) -> tuple[str, bool] | None:
    """(« ~mnbr », exact) to look for in search_norm, or None when too short to be meaningful.

    3+ letters: any stored skeleton starting with it (typing « monop » finds « مونوبري »).
    2 letters from a 5+ letter word: only the same whole skeleton (« aziza » ↔ « عزيزة »)."""
    sk = skeleton(token)
    if len(sk) >= MIN_SKELETON:
        return SKELETON_MARK + sk, False
    if len(sk) == 2 and len(token) >= SHORT_SKELETON_WORD:
        return SKELETON_MARK + sk, True
    return None


def skeletons(*values: object) -> list[str]:
    """The stored skeletons of a text, one per token (duplicates and short ones left out)."""
    seen: list[str] = []
    for token in tokenize(*values):
        if token.isdigit():
            continue  # numbers already match as they are
        sk = skeleton(token)
        if len(sk) >= 2 and SKELETON_MARK + sk not in seen:
            seen.append(SKELETON_MARK + sk)
    return seen


def skeleton_in(query: tuple[str, bool], haystack: str) -> bool:
    """A stored skeleton of `haystack` (search_norm-like text) starts with / is the query one."""
    sk, exact = query
    return f" {sk}{' ' if exact else ''}" in f" {haystack} "
