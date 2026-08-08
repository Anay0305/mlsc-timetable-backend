"""Normalize the room strings the workbook actually contains.

Rooms are typed by hand into the spreadsheet, so the same physical space
arrives in several spellings::

    L307            the bare room number
    AI(L307)        the lab's name with its room in brackets
    GC-2(L107       the same, with the bracket never closed
    HIGH VOLTAGE-C101   a named lab hyphenated onto its room
    B204/F314       one class genuinely running in two rooms
    CBCL/G114       a lab name and its room, separated by a slash
    Not Given       no room at all

Left alone, ``AI(L307)`` and ``L307`` index as two different rooms and the
availability view reports one of them free while the other is occupied — which
is precisely the question the room view exists to answer. Everything here
folds those spellings onto one canonical id and keeps the original string as
the label to display.
"""

from __future__ import annotations

import re
from typing import Any

# A room code: an optional wing prefix then 2–4 digits, optionally suffixed.
# Matches LT102, L004, C328, W121, D112, TA27, G253A.
_ROOM_RE = re.compile(r"^[A-Z]{0,4}-?\d{2,4}[A-Z]?$")
# The same shape when embedded in a longer string ("HIGH VOLTAGE-C101").
_EMBEDDED_ROOM_RE = re.compile(r"[A-Z]{1,4}\d{2,4}[A-Z]?")
# Bracketed room, tolerating the missing closing bracket seen in the source.
_BRACKET_RE = re.compile(r"\(([^)]+)\)?")

_PLACEHOLDERS = frozenset({"", "?", "-", "--", "NA", "N/A", "TBA", "TBD", "NOT GIVEN", "NONE"})
_SPLIT_RE = re.compile(r"[/,&]")


def _clean_token(value: str) -> str:
    """Trim padding and trailing punctuation ("E311." -> "E311")."""
    return re.sub(r"\s+", " ", value).strip().strip(".,;:").strip()


def _canonical_part(part: str) -> str | None:
    """Reduce one slash-separated fragment to a room id, if it has one."""
    token = _clean_token(part)
    if not token or token.upper() in _PLACEHOLDERS:
        return None
    upper = token.upper()

    # "AI(L307)" / "GC-2(L107" -> the bracketed room wins over the lab name.
    for inner in _BRACKET_RE.findall(upper):
        candidate = _clean_token(inner)
        if _ROOM_RE.match(candidate):
            return candidate
        embedded = _EMBEDDED_ROOM_RE.search(candidate)
        if embedded:
            return embedded.group(0)

    # Drop any bracketed remainder before testing the bare token.
    bare = _clean_token(_BRACKET_RE.sub("", upper))
    if _ROOM_RE.match(bare):
        return bare

    # "HIGH VOLTAGE-C101", "DS-19L004)" -> pull the room out of the noise.
    embedded = _EMBEDDED_ROOM_RE.search(bare)
    if embedded:
        return embedded.group(0)

    # A genuinely named space with no number ("BAJAJ LAB"). Keep it: it is a
    # real bookable room, just not a numbered one.
    return bare or None


def normalize_room(value: Any) -> tuple[list[str], str | None]:
    """Return ``(canonical_room_ids, display_label)`` for one room string.

    Multiple ids come back only when a class genuinely occupies more than one
    room. ``CBCL/G114`` is a lab name beside its room, not two rooms, so it
    yields one id — the rule being that if some fragments look like room codes
    and others do not, only the room-shaped ones are real.
    """
    raw = str(value or "").strip()
    if not raw or raw.upper() in _PLACEHOLDERS:
        return [], None

    parts = [part for part in _SPLIT_RE.split(raw) if _clean_token(part)]
    resolved = [(part, _canonical_part(part)) for part in parts]
    resolved = [(part, room) for part, room in resolved if room]
    if not resolved:
        return [], None

    room_shaped = [(part, room) for part, room in resolved if _ROOM_RE.match(room)]
    # "B204/F314" is two rooms; "CBCL/G114" is a label and a room.
    chosen = room_shaped if room_shaped else resolved

    ids: list[str] = []
    for _, room in chosen:
        if room not in ids:
            ids.append(room)

    label = _clean_token(raw).upper()
    return ids, label or None
