"""Objectionable-word filter for what users write to each other (App Review 1.2: "a method for
filtering objectionable material"): the order chat and the message of an offer.

A word of the list is masked with asterisks of the same length, the rest of the text is kept.
The list holds strong insults only (French, Tunisian derja in Latin letters, Arabic), so normal
words are never touched. Matching ignores case and accents and reads the usual digit spellings
of letters in French ("c0nnard"); derja digits (3, 7, 9) are letters and stay as written.
"""

import re
import unicodedata

_FR = {
    "connard", "connards", "connasse", "salope", "salopes", "salaud", "encule", "encules", "enculer",
    "pute", "putes", "putain", "nique", "niquer", "niquez", "ntm", "fdp", "batard", "batards",
    "pede", "pedes", "tapette", "enfoire", "enfoires", "pd", "bite", "couille", "couilles",
    "merde", "chier", "abruti", "abrutie", "debile", "pouffiasse", "trouduc", "tg",
}  # fmt: skip
_DERJA = {
    "zebi", "zeby", "zab", "zabour", "nayek", "nayk", "nik", "nikomek", "nikmok", "kahba", "9a7ba",
    "qahba", "kahbe", "miboun", "mibboun", "zok", "zokk", "zokomek", "tahan",
    # « manyak » and its usual spellings (e/i/ou, with or without the first a, plural / feminine)
    "manyak", "manyek", "manyik", "manyok", "manyouk", "maniouk", "manyaka", "manyka", "manyouka",
    "mnayek", "mnayak", "mnayik", "mnaykia", "mnayka", "mnaike", "mnayeik",
    "3ahra", "khra", "khara", "zamel", "zemel",
}  # fmt: skip
_AR = {
    "قحبة", "كحبة", "قحاب", "زبي", "زب", "زبور", "نيك", "نيكمك", "زك", "زكمك", "ميبون", "طحان",
    "شرموطة", "شرموط", "منيوك", "منيك", "منياك", "مانياك", "مانيك", "منايك", "عاهرة", "خرا", "زامل",
}  # fmt: skip
BLOCKED_WORDS = frozenset(_FR | _DERJA | _AR)

# letters and digits (derja), Arabic letters included; apostrophes split words ("l'enculé")
_WORD = re.compile("(?:[^\\W_]|[\\u064B-\\u0652\\u0640])+")  # + harakat / tatweel inside a word
_LEET = str.maketrans({"0": "o", "1": "i", "@": "a", "$": "s"})
_ARABIC_DIACRITICS = re.compile("[\\u064B-\\u0652\\u0640]")  # harakat + tatweel


def _normal(word: str) -> str:
    word = _ARABIC_DIACRITICS.sub("", word.lower())
    stripped = "".join(c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn")
    # the same word with long repeated letters ("connaaard") counts too
    squeezed = re.sub(r"(.)\1{2,}", r"\1", stripped)
    return squeezed


def _blocked(word: str) -> bool:
    normal = _normal(word)
    if normal in BLOCKED_WORDS:
        return True
    # French digit spellings, never on derja words that use digits as letters
    leet = normal.translate(_LEET)
    return leet != normal and leet in _FR


def mask(text: str) -> tuple[str, bool]:
    """(the text with blocked words masked, whether anything was masked)."""
    if not text:
        return text, False
    found = False

    def repl(match: re.Match[str]) -> str:
        nonlocal found
        word = match.group(0)
        if _blocked(word):
            found = True
            return "*" * len(word)
        return word

    return _WORD.sub(repl, text), found
