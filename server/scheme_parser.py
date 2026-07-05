"""Extract baseline course tables from a Thapar B.E. course-scheme PDF.

This module powers both the CLI at ``scripts/parse_course_scheme.py`` and the
``POST /admin/scheme/*`` admin endpoints. It has no side effects and no I/O
outside of opening the PDF given to :func:`parse_scheme_pdf`.

The keyline convention is:

* Odd semesters   -> ``O<year>``  (Sem 1 = O1, Sem 3 = O2, ...)
* Even semesters  -> ``E<year>``  (Sem 2 = E1, Sem 4 = E2, ...)

Pool A/B rotation (Sem 1 curriculum = O1A + E1B for pool-swap branches, straight
O1/E1 for X/G/J/R and second-year-onwards) is a downstream concern applied when
mapping semesters to baseline keys — not encoded in the PDF itself.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pdfplumber

SEMESTER_HEADER_RE = re.compile(
    r"^\s*SEMESTER[-\s]*(I{1,3}|IV|V|VI{1,3}|VIII)\s*$", re.I
)
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4,
         "V": 5, "VI": 6, "VII": 7, "VIII": 8}

FIELD_ALIASES = {
    "sn": {"s.no.", "s. no.", "s.n.", "s. n.", "sr.no.", "sno"},
    "code": {"course code", "course no.", "coursecode", "courseno."},
    "title": {"course name", "title"},
    "category": {"code", "code**"},
    "L": {"l"},
    "T": {"t"},
    "P": {"p"},
    "Cr": {"cr", "cr.", "credits", "credit"},
}

COURSE_CODE_RE = re.compile(r"^U[A-Z]{2,3}[A-Z0-9]{3,4}$")


@dataclass
class Course:
    sn: str | None
    code: str | None
    title: str
    category: str | None
    L: str | None
    T: str | None
    P: str | None
    Cr: str | None


@dataclass
class Semester:
    number: int
    keyline: str
    year: int
    options: list[dict[str, Any]] = field(default_factory=list)


# ── low-level helpers ────────────────────────────────────────────────────
def _norm(cell: Any) -> str:
    if cell is None:
        return ""
    return str(cell).replace("\n", " ").strip()


def _build_column_map(header_rows: list[list[Any]]) -> dict[str, list[int]]:
    """Return ``{field: [col_index, …]}`` after joining multi-row headers and
    extending each labeled column rightward into its unlabeled neighbours (a
    single visual header cell is often split into 2+ sub-columns).
    """
    if not header_rows:
        return {}
    width = max(len(r) for r in header_rows)
    joined: list[str] = []
    for c in range(width):
        parts = [_norm(r[c]) for r in header_rows if c < len(r) and _norm(r[c])]
        joined.append(" ".join(parts).lower())

    field_of: list[str | None] = [None] * width
    for c, label in enumerate(joined):
        if not label:
            continue
        for field_name, aliases in FIELD_ALIASES.items():
            if label in aliases:
                field_of[c] = field_name
                break

    col_map: dict[str, list[int]] = {f: [] for f in FIELD_ALIASES}
    c = 0
    while c < width:
        owner = field_of[c]
        if owner is None:
            c += 1
            continue
        col_map[owner].append(c)
        j = c + 1
        while j < width and field_of[j] is None:
            col_map[owner].append(j)
            j += 1
        c = j
    return col_map


def _is_header_row(row: list[Any]) -> bool:
    tokens = " ".join(_norm(c) for c in row).lower().split()
    return ("cr" in tokens or "credits" in tokens) and (
        "l" in tokens and "t" in tokens and "p" in tokens
    )


def _looks_like_header_start(row: list[Any]) -> bool:
    tokens = " ".join(_norm(c) for c in row).lower().split()
    return "title" in tokens and ("code" in tokens or "l" in tokens)


def _find_header_span(rows: list[list[Any]]) -> tuple[int, int]:
    """Return ``(start, end_exclusive)`` covering the header block."""
    for i, r in enumerate(rows):
        combined_ok = _is_header_row(r)
        if not combined_ok and _looks_like_header_start(r) and i + 1 < len(rows):
            merged = [
                (_norm(a) + " " + _norm(b)).strip()
                for a, b in zip(r, rows[i + 1])
            ]
            combined_ok = _is_header_row(merged)
        if combined_ok:
            end = i + 1
            while end < len(rows) and end < i + 3:
                joined = " ".join(_norm(c) for c in rows[end]).strip()
                if joined and not any(COURSE_CODE_RE.match(_norm(c)) for c in rows[end]) \
                        and len(joined) < 40 and _norm(rows[end][0]) in {"", "S.", "No", "No.", "NO."}:
                    end += 1
                    continue
                if joined and joined.lower() in {"course", "code", "no.", "s. no.", "no", "s. n."}:
                    end += 1
                    continue
                break
            return i, end
    return -1, -1


def _extract_courses_from_table(
    table: list[list[Any]],
) -> tuple[list[Course], dict[str, str | None]]:
    """Convert a raw pdfplumber table into Course rows + the printed TOTAL row."""
    hdr_start, hdr_end = _find_header_span(table)
    if hdr_start < 0:
        return [], {}
    col_map = _build_column_map(table[hdr_start:hdr_end])
    required = {"title", "L", "T", "P", "Cr"}
    if not all(col_map.get(k) for k in required):
        return [], {}

    def pick_from(row: list[Any], field_name: str) -> str:
        for idx in col_map.get(field_name, ()):
            if idx < len(row):
                val = _norm(row[idx])
                if val:
                    return val
        return ""

    courses: list[Course] = []
    printed_totals: dict[str, str | None] = {"L": None, "T": None, "P": None, "Cr": None}

    for raw in table[hdr_end:]:
        if not any(_norm(c) for c in raw):
            continue
        cell_tokens = {_norm(c).lower() for c in raw if _norm(c)}
        if "total" in cell_tokens:
            for k in ("L", "T", "P", "Cr"):
                v = pick_from(raw, k)
                printed_totals[k] = v or None
            break
        joined_lc = " ".join(_norm(c) for c in raw).lower()
        if joined_lc.startswith("note") or joined_lc.startswith("*"):
            continue

        sn = pick_from(raw, "sn")
        code = pick_from(raw, "code")
        title = pick_from(raw, "title")
        category = pick_from(raw, "category")
        L, T, P, Cr = (pick_from(raw, k) for k in ("L", "T", "P", "Cr"))

        is_pure_continuation = (not sn and not code and not category
                                and not L and not T and not P and not Cr
                                and title)
        is_code_repeat = (
            courses
            and code
            and courses[-1].code == code
        )

        if (is_pure_continuation or is_code_repeat) and courses:
            if title:
                courses[-1].title = (courses[-1].title + " " + title).strip()
            continue

        if not code:
            for cell in raw:
                token = _norm(cell)
                if COURSE_CODE_RE.match(token):
                    code = token
                    break

        if not (title or code):
            continue

        courses.append(Course(
            sn=sn or None,
            code=code or None,
            title=title,
            category=category or None,
            L=L or None, T=T or None, P=P or None, Cr=Cr or None,
        ))
    return courses, printed_totals


# ── semester + keyline detection ─────────────────────────────────────────
_SEM_WORD_RE = re.compile(r"^SEMESTER[-\s]*(I{1,3}|IV|V|VI{1,3}|VIII)$", re.I)


def _semester_anchors(page) -> list[tuple[float, int]]:
    anchors: list[tuple[float, int]] = []
    for w in page.extract_words():
        m = _SEM_WORD_RE.match(w["text"].strip())
        if not m:
            continue
        roman = m.group(1).upper()
        if roman in ROMAN:
            anchors.append((float(w["top"]), ROMAN[roman]))
    anchors.sort()
    return anchors


def _sem_for_table(table_top: float, anchors: list[tuple[float, int]]) -> int | None:
    candidate: int | None = None
    for y, sem in anchors:
        if y <= table_top:
            candidate = sem
        else:
            break
    return candidate


def _sem_from_table_body(table: list[list[Any]]) -> int | None:
    for row in table:
        for cell in row:
            m = _SEM_WORD_RE.match(_norm(cell))
            if m and m.group(1).upper() in ROMAN:
                return ROMAN[m.group(1).upper()]
    return None


def keyline_for(sem: int) -> tuple[str, int]:
    """Return ``(keyline, year)`` — e.g. ``('O2', 2)`` for Sem 3."""
    year = (sem + 1) // 2
    prefix = "O" if sem % 2 == 1 else "E"
    return f"{prefix}{year}", year


def baseline_key_for(
    sem: int,
    branch: str,
    *,
    pool_swap_year1: bool = False,
) -> str:
    """Build a baseline key like ``O2C`` for (Sem 3, branch C).

    When ``pool_swap_year1`` is true the parity of year-1 semesters is
    flipped, so Sem 1 gets an ``E`` prefix and Sem 2 gets an ``O`` prefix.
    Used for the pool-B branches whose year-1 curriculum is swapped.
    """
    branch = (branch or "").strip().upper()
    if len(branch) != 1 or not branch.isalpha():
        raise ValueError(
            f"invalid branch code {branch!r}: must be a single letter A–Z"
        )
    year = (sem + 1) // 2
    is_odd = sem % 2 == 1
    if pool_swap_year1 and year == 1:
        is_odd = not is_odd
    prefix = "O" if is_odd else "E"
    return f"{prefix}{year}{branch}"


# ── totals helpers ───────────────────────────────────────────────────────
_NUM_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")


def _numeric(value: str | None) -> float:
    if not value:
        return 0.0
    return sum(float(t) for t in _NUM_TOKEN_RE.findall(value))


def _fmt_total(x: float) -> float | int:
    return int(x) if x == int(x) else round(x, 2)


def _computed_totals(courses: list[Course]) -> dict[str, float | int]:
    return {
        k: _fmt_total(sum(_numeric(getattr(c, k)) for c in courses))
        for k in ("L", "T", "P", "Cr")
    }


# ── public entrypoint ────────────────────────────────────────────────────
def parse_scheme_pdf(pdf_path: str | Path) -> dict[str, Any]:
    """Parse a course-scheme PDF and return a structured description.

    Shape::

        {
          "source": "BECSE2025_Scheme.pdf",
          "semester_count": 8,
          "keyline_convention": { "notes": [...], "sem_to_keyline": {...} },
          "semesters": [
            {
              "number": 1, "keyline": "O1", "year": 1,
              "options": [
                { "courses": [...], "totals": { "printed": {...}, "computed": {...} } }
              ]
            }, ...
          ]
        }
    """
    pdf_path = Path(pdf_path)
    semesters: dict[int, Semester] = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            anchors = _semester_anchors(page)
            if not anchors:
                continue

            for ft in page.find_tables():
                table = ft.extract()
                if not table:
                    continue
                if _find_header_span(table)[0] < 0:
                    continue
                courses, printed_totals = _extract_courses_from_table(table)
                if not courses:
                    continue
                sem_num = _sem_from_table_body(table) or _sem_for_table(ft.bbox[1], anchors)
                if sem_num is None:
                    continue

                if sem_num not in semesters:
                    keyline, year = keyline_for(sem_num)
                    semesters[sem_num] = Semester(number=sem_num, keyline=keyline, year=year)

                semesters[sem_num].options.append({
                    "courses": [asdict(c) for c in courses],
                    "totals": {
                        "printed": printed_totals,
                        "computed": _computed_totals(courses),
                    },
                })

    ordered = [semesters[k] for k in sorted(semesters)]
    return {
        "source": pdf_path.name,
        "semester_count": len(ordered),
        "keyline_convention": {
            "notes": [
                "Odd sems -> O<year>, Even sems -> E<year>.",
                "Pool A/B branches: Sem 1 curriculum = O1A + E1B (swapped for pool B).",
                "Exempt from pool swap (year 1 straight O1/E1): X, G, J, R.",
            ],
            "sem_to_keyline": {i: keyline_for(i)[0] for i in range(1, 9)},
        },
        "semesters": [asdict(s) for s in ordered],
    }
