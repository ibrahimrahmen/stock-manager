"""
Angry-customer detection for DM conversations.

A conversation is flagged "angry" when it contains any word/phrase from
ANGRY_WORDS below. This is a cheap keyword scan — NO AI / API cost.

HOW TO EDIT THE LIST
--------------------
Just add or remove strings in ANGRY_WORDS. Guidelines:
  * Latin / arabizi words (e.g. "kelb", "9a7ba", "escro") are matched as
    WHOLE WORDS, so "nul" would NOT match inside "annuler". Keep them lowercase.
  * Arabic-script words (e.g. "كلب") and multi-word phrases (e.g. "hasbi allah")
    are matched anywhere in the text (substring), so Arabic prefixes like
    "و/ال/ب" don't break the match.
Everything is case-insensitive.
"""

import re

ANGRY_WORDS = [
    # ---- "Hasibiya Allah" family (grievance / calling on God when wronged) ----
    "حسبي الله",
    "حسبي الله ونعم الوكيل",
    "hasbi allah",
    "hasbiya allah",
    "7asbi allah",
    "hasbi rabi",
    "hasbi rabbi",
    "7asbi rabbi",
    # ---- Insults / profanity (arabizi) ----
    "9a7ba", "9a7be", "9hab", "9ahba",
    "kelb", "klab",
    "5anzir", "khanzir", "5anzira",
    "nik", "nayek", "menyek", "manyouk",
    "zebi", "zabbi",
    "fashel", "fechel",
    "7imar", "himar", "bhim", "bhima",
    "3ar",
    "zeft", "zift",
    # ---- Insults / profanity (Arabic script) ----
    "كلب", "خنزير", "قحبة", "نيك", "منيك", "زبي",
    "حمار", "بهيم", "زفت", "عار",
    # ---- Scam / theft accusations ----
    "escro", "escroc", "escroquerie", "escroquer",
    "voleur", "voleurs", "voleuse",
    "sar9a", "sr9a", "sar9in", "nas9in",
    "7ram", "haram",
    "arnaque", "arnaqueur", "arnaquer",
    "سرقة", "سراق", "نصاب", "حرام",
    # ---- Threats / complaints ----
    "nechki", "nchki", "nechkikom",
    "me7kma", "mahkma", "tribunal",
    "police", "شرطة", "محكمة", "نشكي",
    "bel9anon", "9anon",
    # ---- French anger ----
    "merde", "connard", "honte", "inadmissible",
    "voleur", "arnaque", "scandale", "inacceptable",
]

# Build fast lookup structures once at import.
_ARABIC_RANGE = ("؀", "ۿ")


def _has_arabic(s):
    return any(_ARABIC_RANGE[0] <= c <= _ARABIC_RANGE[1] for c in s)


# Latin/arabizi single words → matched as whole tokens.
# Arabic-script words or multi-word phrases → matched as substrings.
_TOKEN_WORDS = set()
_SUBSTRING_WORDS = []
for _w in ANGRY_WORDS:
    _wl = _w.strip().lower()
    if not _wl:
        continue
    if _has_arabic(_wl) or " " in _wl:
        _SUBSTRING_WORDS.append(_wl)
    else:
        _TOKEN_WORDS.add(_wl)

# Split on whitespace and common punctuation; keeps digits (arabizi like 9a7ba).
_TOKEN_RE = re.compile(r"[^\s،؛.,!?;:()\[\]\"'«»/\\+*=_-]+")


def detect_angry(text):
    """Return True if the text contains any angry word/phrase. Never raises."""
    try:
        if not text:
            return False
        low = str(text).lower()
        for phrase in _SUBSTRING_WORDS:
            if phrase in low:
                return True
        tokens = set(_TOKEN_RE.findall(low))
        return bool(tokens & _TOKEN_WORDS)
    except Exception:
        return False
