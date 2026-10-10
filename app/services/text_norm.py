"""Text normalization of the place search.

One tokenizer for every search path (searchPlaces, searchByBbox, geocodeAddress) and for
the precomputed `places.name_norm` / `places.search_norm` columns, so a query token and
the stored text are always normalized the same way:

    lower case → NFD → combining accents (U+0300–U+036F) removed → split on anything
    that is not a Unicode letter or digit (JS `/[^\\p{L}\\p{N}]+/u`).

Arabic stays searchable (no ASCII-only `\\w`, which would empty Arabic names);
Arabic short vowels (harakat, U+064B–U+065F, U+0670) and the tatweel (U+0640) are also
removed so a vocalized spelling matches a bare one.
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
    return normalize_text(name, address, city)
