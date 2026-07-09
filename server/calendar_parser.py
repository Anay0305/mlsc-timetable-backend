"""Extract semester-calendar overrides from a Thapar academic-calendar PDF.

The parser is intentionally shallow: it does NOT re-derive weekday
indices from the grid or try to reconcile "H"/"NT" suffixes on
individual date cells. Instead it trusts three sources on the PDF:

    1. Section "H : Gazetted Holidays"
         → per-date holiday name (source of truth for holidays)
    2. Section "NT (Non-Teaching)"
         → dates that are cancelled and compensated later
    3. Section "Teaching Days in Lieu of NT"
         → Saturday(s) that follow another weekday's timetable

Plus one grid-level signal: whether a week's *phase* label reads
``Teaching`` / ``MST`` / ``EST`` / ``Assessment`` / ``Diwali``. Diwali
weekdays become holidays; MST / EST / Assessment weekdays become their
own override kinds so the frontend can colour-code them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pdfplumber

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

FOLLOWS_DAY_INDEX = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
}

MONTH_ABBR = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

YEAR_TITLE_RE = re.compile(
    r"(ODD|EVEN)\s+SEM\s+(\d{4})[\s\-]+(\d{2,4})", re.I,
)
WEEK_HEADER_RE = re.compile(r"Week\s*-?\s*(\d+)\s*\(([^)]+)\)", re.I)

# "04/09/2026 -- Janamashtmi"
LEGEND_H_RE = re.compile(
    r"(\d{2})/(\d{2})/(\d{4})\s*--\s*([^\d/]+?)(?=\s+\d{2}/\d{2}/\d{4}|$)",
    re.MULTILINE,
)
NT_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
# "31st Oct in lieu of 19th Oct. with Monday's Timetable"
LIEU_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)\.?\s+in\s+lieu\s+of\s+"
    r"(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)\.?"
    r"(?:\s+with\s+([A-Za-z]+)(?:'s|\u2019s)?\s+Timetable)?",
    re.I,
)
# Alternate form seen on shorter calendars:
#   "18 April (in lieu of April 13) :Monday Time table"
# Groups: on_day, on_month_name, replaces_month_name, replaces_day, follows_name
LIEU_PAREN_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s*\(\s*in\s+lieu\s+of\s+([A-Za-z]+)\s+(\d{1,2})\s*\)"
    r"\s*:?\s*([A-Za-z]+)?\s*Time\s*table",
    re.I,
)

PHASE_TEACHING = "teaching"
PHASE_MST = "mst"
PHASE_EST = "est"
PHASE_ASSESSMENT = "assessment"
PHASE_DIWALI = "diwali"
PHASE_NT_WEEK = "nt_week"


def _classify_phase(raw: str) -> str:
    s = (raw or "").strip().lower()
    if not s or s.startswith("teach"):
        return PHASE_TEACHING
    if "non-teaching" in s or "non teaching" in s or "(nt)" in s:
        return PHASE_NT_WEEK
    if "diwali" in s:
        return PHASE_DIWALI
    if "mst" in s:
        return PHASE_MST
    if "est" in s:
        return PHASE_EST
    if "assessment" in s or "evaluation" in s:
        return PHASE_ASSESSMENT
    return PHASE_TEACHING


@dataclass
class WeekCell:
    weekday: str
    date: Optional[str]      # 'YYYY-MM-DD' or None (X)
    suffix: Optional[str]    # 'H' / 'NT' / None
    raw: str


@dataclass
class WeekBlock:
    number: int
    month_hint: str
    phase: str
    phase_raw: str
    cells: list[WeekCell] = field(default_factory=list)


@dataclass
class ParsedCalendar:
    source: str
    year_start: int
    year_end: int
    sem_kind: str
    weeks: list[WeekBlock]
    overrides: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    holiday_legend: list[dict[str, str]]
    non_teaching: list[str]
    lieu_mappings: list[dict[str, Any]]
    suggested_term_end: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "year_start": self.year_start,
            "year_end": self.year_end,
            "sem_kind": self.sem_kind,
            "suggested_term_end": self.suggested_term_end,
            "weeks": [
                {
                    "number": w.number,
                    "month_hint": w.month_hint,
                    "phase": w.phase,
                    "phase_raw": w.phase_raw,
                    "cells": [
                        {"weekday": c.weekday, "date": c.date,
                         "suffix": c.suffix, "raw": c.raw}
                        for c in w.cells
                    ],
                }
                for w in self.weeks
            ],
            "overrides": self.overrides,
            "warnings": self.warnings,
            "holiday_legend": self.holiday_legend,
            "non_teaching": self.non_teaching,
            "lieu_mappings": self.lieu_mappings,
        }


def _pad(n: int) -> str:
    return f"{n:02d}"


def _month_from_hint(hint: str) -> Optional[int]:
    """Return the first month named in a header like 'Sept-Oct' → 9."""
    for tok in re.findall(r"[A-Za-z]+", hint or ""):
        m = MONTH_ABBR.get(tok.lower())
        if m:
            return m
    return None


def _parse_cell(raw: str) -> tuple[Optional[int], Optional[str]]:
    """'27' → (27, None); '19-NT' → (19, 'NT'); 'X' / '' → (None, None)."""
    if not raw or raw.strip().upper() == "X":
        return (None, None)
    m = re.match(r"(\d{1,2})(?:-([A-Za-z]+))?", raw.strip())
    if not m:
        return (None, None)
    return int(m.group(1)), (m.group(2).upper() if m.group(2) else None)


def _weeks_from_tables(pdf) -> list[WeekBlock]:
    """Walk pdfplumber tables and produce one WeekBlock per week.

    Each page's table lays out 4 week-blocks side-by-side: for every
    row-group we look at the *weekday-label* row (contains 'Mon') to
    find each block's starting column, then read the header (row above),
    dates (row below), and phase (2 rows below). This lets us pick up
    blocks whose header cell says something other than 'Week -N' — the
    Diwali/legend column often does exactly that.
    """
    blocks: list[WeekBlock] = []
    seen_keys: set[tuple[int, str, tuple[str, ...]]] = set()
    for page in pdf.pages:
        for tbl in page.extract_tables() or []:
            for i in range(len(tbl) - 2):
                weekday_row = tbl[i]
                mon_cols = [
                    j for j, c in enumerate(weekday_row)
                    if (str(c or "").strip().lower() == "mon")
                ]
                if not mon_cols:
                    continue
                header_row = tbl[i - 1] if i - 1 >= 0 else []
                date_row = tbl[i + 1] if i + 1 < len(tbl) else []
                phase_row = tbl[i + 2] if i + 2 < len(tbl) else []

                for col in mon_cols:
                    header_cell = ""
                    if col < len(header_row) and header_row[col]:
                        header_cell = str(header_row[col]).strip()
                    m = WEEK_HEADER_RE.search(header_cell)
                    num = int(m.group(1)) if m else 0
                    hint = m.group(2).strip() if m else ""

                    cells: list[WeekCell] = []
                    for k in range(6):
                        raw = ""
                        if col + k < len(date_row) and date_row[col + k]:
                            raw = str(date_row[col + k]).strip()
                        wd_label = WEEKDAYS[k]
                        if col + k < len(weekday_row) and weekday_row[col + k]:
                            wd_label = str(weekday_row[col + k]).strip() or WEEKDAYS[k]
                        day, suf = _parse_cell(raw)
                        cells.append(WeekCell(
                            weekday=wd_label,
                            date=str(day) if day is not None else None,
                            suffix=suf,
                            raw=raw,
                        ))
                    if not any(c.date for c in cells):
                        continue

                    phase_raw = ""
                    if col < len(phase_row) and phase_row[col]:
                        phase_raw = str(phase_row[col]).strip()

                    key = (num, hint, tuple(c.date or "" for c in cells))
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    blocks.append(WeekBlock(
                        number=num,
                        month_hint=hint,
                        phase=_classify_phase(phase_raw),
                        phase_raw=phase_raw,
                        cells=cells,
                    ))
    return blocks


def _resolve_dates(blocks: list[WeekBlock], year_start: int, year_end: int) -> None:
    """Fill WeekCell.date with 'YYYY-MM-DD' strings.

    The header hint (e.g. ``Dec`` or ``Sept-Oct``) names the *majority*
    month(s) of the week. If the date sequence breaks (e.g. ``30 1 2 3
    4 5``), the shorter segment belongs to the adjacent month — which
    side depends on which segment is longer. For header-less blocks
    (the Diwali column) we fall back to a running cursor.
    """
    def _year_for(m: int) -> int:
        return year_start if m >= 7 else year_end

    prev_end_month: Optional[int] = None
    prev_end_year: Optional[int] = None
    prev_end_day: Optional[int] = None

    for w in blocks:
        # (original_index_in_cells, day_int) for every non-blank cell.
        indexed = [(i, int(c.date)) for i, c in enumerate(w.cells)
                   if c.date is not None]
        if not indexed:
            continue

        # Find break: first index k where day decreases.
        break_k: Optional[int] = None
        for k in range(1, len(indexed)):
            if indexed[k][1] < indexed[k - 1][1]:
                break_k = k
                break

        # Months named in the header hint, in printed order.
        hint_months: list[int] = []
        for tok in re.findall(r"[A-Za-z]+", w.month_hint or ""):
            m = MONTH_ABBR.get(tok.lower())
            if m:
                hint_months.append(m)

        # Decide (month, year) for the two possible segments.
        seg_before: Optional[tuple[int, int]] = None
        seg_after: Optional[tuple[int, int]] = None

        if hint_months:
            if len(hint_months) >= 2 and break_k is not None:
                m1, m2 = hint_months[0], hint_months[1]
                y1 = _year_for(m1)
                y2 = _year_for(m2)
                if m1 == 12 and m2 == 1:
                    y2 = y1 + 1
                seg_before, seg_after = (m1, y1), (m2, y2)
            else:
                # Single-month hint (or two months but no break).
                hint_m = hint_months[0]
                hint_y = _year_for(hint_m)
                if break_k is None:
                    seg_before = (hint_m, hint_y)
                else:
                    len_before = break_k
                    len_after = len(indexed) - break_k
                    # Longer segment is the *majority* month = hint.
                    if len_after >= len_before:
                        prev_m = 12 if hint_m == 1 else hint_m - 1
                        prev_y = hint_y - 1 if hint_m == 1 else hint_y
                        seg_before = (prev_m, prev_y)
                        seg_after = (hint_m, hint_y)
                    else:
                        next_m = 1 if hint_m == 12 else hint_m + 1
                        next_y = hint_y + 1 if hint_m == 12 else hint_y
                        seg_before = (hint_m, hint_y)
                        seg_after = (next_m, next_y)
        else:
            # Header-less: rely on the running cursor.
            if prev_end_month is None:
                cur_m, cur_y = 1, year_start
            else:
                cur_m, cur_y = prev_end_month, prev_end_year or year_start
            first_day = indexed[0][1]
            if prev_end_day is not None and first_day < prev_end_day:
                cur_m = 1 if cur_m == 12 else cur_m + 1
                if cur_m == 1:
                    cur_y = (cur_y or year_start) + 1
            seg_before = (cur_m, cur_y)
            if break_k is not None:
                nxt_m = 1 if cur_m == 12 else cur_m + 1
                nxt_y = cur_y + 1 if cur_m == 12 else cur_y
                seg_after = (nxt_m, nxt_y)

        # Write ISO dates for every non-blank cell.
        for k, (orig, day) in enumerate(indexed):
            m, y = (seg_after if (break_k is not None and k >= break_k) else seg_before)
            w.cells[orig].date = f"{y:04d}-{m:02d}-{day:02d}"

        last_m, last_y = (seg_after if seg_after and break_k is not None else seg_before)
        prev_end_month = last_m
        prev_end_year = last_y
        prev_end_day = indexed[-1][1]


def _parse_holiday_legend(text: str) -> list[tuple[str, str]]:
    """Return every ``dd/mm/yyyy -- name`` entry in the legend area.

    The 'H: Gazetted Holidays' and 'NT (Non-Teaching)' columns sit
    side-by-side on the PDF so their text is interleaved. We scan from
    the H heading down to 'Note:' (or EOF) and pick up every date-name
    pair — both true H holidays and NT-with-name entries are treated
    the same way (both are days classes don't happen).
    """
    start = re.search(r"H\s*:\s*Gazetted\s+Holidays", text, re.I)
    tail = text[start.end():] if start else text
    end = re.search(r"Note\s*:", tail, re.I)
    block = tail[: end.start()] if end else tail
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in LEGEND_H_RE.finditer(block):
        dd, mm, yyyy, name = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            iso = f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            continue
        if iso in seen:
            continue
        cleaned = re.sub(r"\s+", " ", name).strip(" -")
        # Drop trailing 'Teaching Days in Lieu of NT' if the regex ate
        # too much of the section footer.
        cleaned = re.sub(r"\s+Teaching\s+Days.*$", "", cleaned, flags=re.I)
        if cleaned:
            out.append((iso, cleaned))
            seen.add(iso)
    return out


def _parse_nt_dates(text: str, exclude: set[str]) -> list[str]:
    """Bare 'dd/mm/yyyy' tokens (no '--' after) in the legend section."""
    start = re.search(r"NT\s*\(Non-Teaching\)", text, re.I)
    tail = text[start.end():] if start else text
    end = re.search(r"Note\s*:", tail, re.I)
    block = tail[: end.start()] if end else tail
    out: list[str] = []
    for m in NT_DATE_RE.finditer(block):
        # Skip dates that are the LHS of a '-- name' pair.
        after = block[m.end(): m.end() + 4]
        if after.lstrip().startswith("--"):
            continue
        dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        try:
            iso = f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
        except ValueError:
            continue
        if iso in exclude or iso in out:
            continue
        out.append(iso)
    return out


def _parse_lieu_mappings(text: str, year_start: int,
                         year_end: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Returns (mappings, unresolved_raw_lines).

    Scans the whole PDF text — the calendar sometimes buries a second
    lieu rule inside the ``Note:`` footer line, so we deliberately don't
    stop at that boundary.
    """
    mappings: list[dict[str, Any]] = []
    unresolved: list[str] = []
    seen: set[str] = set()
    for m in LIEU_RE.finditer(text):
        on_day = int(m.group(1))
        on_month_name = m.group(2)
        replaces_day = int(m.group(3))
        replaces_month_name = m.group(4)
        follows_name = (m.group(5) or "").strip().lower()

        on_month = MONTH_ABBR.get(on_month_name.lower())
        replaces_month = MONTH_ABBR.get(replaces_month_name.lower())
        follows_idx = FOLLOWS_DAY_INDEX.get(follows_name)
        if not on_month:
            continue

        on_year = year_start if on_month >= 7 else year_end
        on_iso = f"{on_year:04d}-{on_month:02d}-{on_day:02d}"
        if on_iso in seen:
            continue
        seen.add(on_iso)

        if follows_idx is None:
            unresolved.append(m.group(0))
            continue

        replaces_year = year_start if (replaces_month or 1) >= 7 else year_end
        rep_iso = (
            f"{replaces_year:04d}-{replaces_month:02d}-{replaces_day:02d}"
            if replaces_month else None
        )
        mappings.append({
            "on_date": on_iso,
            "replaces_date": rep_iso,
            "follows_day": follows_idx,
            "follows_day_name": follows_name.title(),
            "raw": m.group(0),
        })

    # Alternate parenthesised form (short calendar).
    for m in LIEU_PAREN_RE.finditer(text):
        on_day = int(m.group(1))
        on_month_name = m.group(2)
        replaces_month_name = m.group(3)
        replaces_day = int(m.group(4))
        follows_name = (m.group(5) or "").strip().lower()

        on_month = MONTH_ABBR.get(on_month_name.lower())
        replaces_month = MONTH_ABBR.get(replaces_month_name.lower())
        if not on_month:
            continue
        on_year = year_start if on_month >= 7 else year_end
        on_iso = f"{on_year:04d}-{on_month:02d}-{on_day:02d}"
        if on_iso in seen:
            continue
        seen.add(on_iso)

        follows_idx = FOLLOWS_DAY_INDEX.get(follows_name)
        if follows_idx is None:
            unresolved.append(m.group(0))
            continue

        replaces_year = year_start if (replaces_month or 1) >= 7 else year_end
        rep_iso = (
            f"{replaces_year:04d}-{replaces_month:02d}-{replaces_day:02d}"
            if replaces_month else None
        )
        mappings.append({
            "on_date": on_iso,
            "replaces_date": rep_iso,
            "follows_day": follows_idx,
            "follows_day_name": follows_name.title(),
            "raw": m.group(0),
        })
    return mappings, unresolved


def parse_calendar_pdf(pdf_path: str | Path) -> dict[str, Any]:
    path = Path(pdf_path)
    with pdfplumber.open(path) as pdf:
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        weeks = _weeks_from_tables(pdf)

    title = YEAR_TITLE_RE.search(text)
    if not title:
        raise ValueError("Couldn't find 'ODD/EVEN SEM YYYY-YY' title in PDF")
    sem_kind = title.group(1).lower()
    year_start = int(title.group(2))
    year_end_raw = int(title.group(3))
    year_end = 2000 + year_end_raw if year_end_raw < 100 else year_end_raw

    _resolve_dates(weeks, year_start, year_end)

    holidays = _parse_holiday_legend(text)
    holiday_iso = {iso for iso, _ in holidays}
    nt_dates = _parse_nt_dates(text, exclude=holiday_iso)
    lieu, unresolved_lieu = _parse_lieu_mappings(text, year_start, year_end)

    seen: set[tuple[str, str]] = set()
    overrides: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def _add(dt: str, kind: str, reason: Optional[str] = None,
             follows_day: Optional[int] = None) -> None:
        key = (dt, kind)
        if key in seen:
            return
        seen.add(key)
        row: dict[str, Any] = {"date": dt, "kind": kind}
        if reason:
            row["reason"] = reason
        if follows_day is not None:
            row["follows_day"] = follows_day
        overrides.append(row)

    for iso, name in holidays:
        _add(iso, "holiday", reason=name)

    lieu_replaces = {l["replaces_date"] for l in lieu if l.get("replaces_date")}
    for iso in nt_dates:
        if iso in lieu_replaces:
            _add(iso, "holiday", reason="Non-teaching (compensated by lieu Saturday)")
        else:
            _add(iso, "holiday", reason="Non-teaching day")

    for l in lieu:
        reason = (
            f"In lieu of {l['replaces_date']}" if l.get("replaces_date")
            else "In lieu of NT week"
        )
        _add(l["on_date"], "follow_day",
             reason=reason, follows_day=l["follows_day"])

    for w in weeks:
        if w.phase == PHASE_NT_WEEK:
            # Every date in a "NON-TEACHING (NT) WEEK" block is off.
            for c in w.cells:
                if c.date:
                    _add(c.date, "holiday", reason="Non-teaching week")
            continue
        if w.phase not in (PHASE_DIWALI, PHASE_MST, PHASE_EST, PHASE_ASSESSMENT):
            continue
        for c in w.cells:
            if not c.date:
                continue
            try:
                d = date.fromisoformat(c.date)
            except ValueError:
                continue
            # Diwali only covers Mon..Fri (Sat is a default off day
            # already). MST / EST / Assessment weeks DO affect Saturdays
            # too — mock/end-sem tests and evaluation sessions run on
            # Sat — so we mark whichever Sat cell the PDF filled with
            # a real date (not 'X').
            if w.phase == PHASE_DIWALI:
                if d.weekday() >= 5:
                    continue
                _add(c.date, "holiday", reason="Diwali break")
            elif w.phase == PHASE_MST:
                _add(c.date, "mst", reason="MST week")
            elif w.phase == PHASE_EST:
                _add(c.date, "est", reason="EST week")
            elif w.phase == PHASE_ASSESSMENT:
                _add(c.date, "assessment",
                     reason="Assessment / Evaluation week")

    # Grid-cell suffix fallback: cells like "26-H" or "13-NT" that aren't
    # named in the legend still deserve an override. Runs LAST so legend
    # names + phase-week kinds take priority via the (date, kind) dedup.
    for w in weeks:
        for c in w.cells:
            if not c.date or not c.suffix:
                continue
            if c.suffix == "H":
                _add(c.date, "holiday", reason="Holiday")
            elif c.suffix == "NT":
                _add(c.date, "holiday", reason="Non-teaching day")

    for raw in unresolved_lieu:
        warnings.append({
            "date": None,
            "kind": "lieu_unresolved",
            "message": (
                f"Lieu entry didn't state which weekday to follow: {raw!r}"
            ),
        })

    overrides.sort(key=lambda r: (r["date"], r["kind"]))

    # Detect the last calendar date in the PDF as a suggested RRULE UNTIL.
    all_dates = [r["date"] for r in overrides if r.get("date")]
    for w in weeks:
        for c in w.cells:
            if c.date:
                all_dates.append(c.date)
    suggested_term_end = max(all_dates) if all_dates else None

    parsed = ParsedCalendar(
        source=path.name,
        year_start=year_start,
        year_end=year_end,
        sem_kind=sem_kind,
        weeks=weeks,
        overrides=overrides,
        warnings=warnings,
        holiday_legend=[{"date": d, "name": n} for d, n in holidays],
        non_teaching=nt_dates,
        lieu_mappings=lieu,
        suggested_term_end=suggested_term_end,
    )
    return parsed.to_dict()
