"""Grapheme clustering and UTF-16 arithmetic -- the mock's two unit systems.

The model (:mod:`mockdocs.model`) counts in **grapheme clusters**: SPEC §2
defines ``Char.cp`` as "one grapheme cluster", and the whole point of the
character-array representation is that a user-visible character is one array
slot. The Docs API counts in **UTF-16 code units**. The adapter converts
between them, and that conversion is exactly what exercises
``gdocs_preview/analysis.py``'s index discipline -- so this module is
deliberately the only place either unit is computed.

Grapheme segmentation here is an *approximation* of UAX #29 built on
``unicodedata`` alone (no ``regex`` dependency): it joins combining marks,
ZWJ sequences, variation selectors, emoji modifiers, enclosing keycaps,
regional-indicator pairs, and CRLF. That covers every cluster shape the
fixtures and generators produce; it is not a conformant UAX #29
implementation and does not need to be, because the mock only has to be
self-consistent and API-faithful about *UTF-16* indexes.
"""

from __future__ import annotations

import unicodedata

ZWJ = "‍"
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)
_VARIATION_SELECTORS_SUPP = range(0xE0100, 0xE01F0)
_EMOJI_MODIFIERS = range(0x1F3FB, 0x1F400)
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)
_COMBINING_KEYCAP = 0x20E3
#: Tag characters used by subdivision flags (e.g. the Scotland flag).
_TAGS = range(0xE0020, 0xE0080)


def _is_extending(ch: str) -> bool:
    """Whether ``ch`` continues the preceding grapheme cluster."""
    code = ord(ch)
    if unicodedata.category(ch) in ("Mn", "Mc", "Me"):
        return True
    if code in _VARIATION_SELECTORS or code in _VARIATION_SELECTORS_SUPP:
        return True
    if code in _EMOJI_MODIFIERS or code in _TAGS:
        return True
    if code == _COMBINING_KEYCAP or ch == ZWJ:
        return True
    return False


def split_graphemes(text: str) -> list[str]:
    """Split ``text`` into grapheme clusters (approximate UAX #29)."""
    clusters: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        cluster = ch
        i += 1
        # CRLF is one cluster.
        if ch == "\r" and i < n and text[i] == "\n":
            cluster += text[i]
            i += 1
            clusters.append(cluster)
            continue
        # Regional indicators pair up into one flag.
        if ord(ch) in _REGIONAL_INDICATORS:
            if i < n and ord(text[i]) in _REGIONAL_INDICATORS:
                cluster += text[i]
                i += 1
            clusters.append(cluster)
            continue
        while i < n:
            nxt = text[i]
            if _is_extending(nxt):
                cluster += nxt
                i += 1
                # A ZWJ glues whatever follows into the same cluster.
                if nxt == ZWJ and i < n:
                    cluster += text[i]
                    i += 1
                continue
            break
        clusters.append(cluster)
    return clusters


def utf16_len(s: str) -> int:
    """Length of ``s`` in UTF-16 code units (the Docs API index unit)."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in s)
