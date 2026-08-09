#!/usr/bin/env python3
"""Look up missing subject titles in the official course-scheme PDFs.

When the Fix queue reports ``SUBJECT_NOT_IN_CATALOG``, the title almost always
exists in a SUGC/SPGC scheme PDF — someone just has to find it. This searches
every scheme PDF for a set of codes and prints the title it found, so the codes
can be added to the catalog without reading twenty documents by hand.

    # codes still unnamed in the live timetables, straight from the database
    python scripts/find_subject_titles.py --from-db

    # or an explicit list, against specific files
    python scripts/find_subject_titles.py UCS762 UCS772 --pdf-dir ../Schema

Pages are pre-filtered on a plain-text scan before the (much slower) table
extraction runs, so a full sweep of the scheme library stays quick.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# A scheme row is "<code> <title> <L> <T> <P> <Cr>"; the title is everything
# between the code and the first numeric column.
_TRAILING_NUMBERS = re.compile(r"\s+[\d\s./*+()-]+$")
_NOISE = re.compile(r"^(course|subject)?\s*(code|title|name)\s*$", re.I)


def clean_title(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip(" .:-|")
    text = _TRAILING_NUMBERS.sub("", text).strip()
    return text


_OTHER_CODE = re.compile(r"\(?\bU[A-Z]{2,4}\d{3}[LTP]?\b\)?")
_STRUCTURAL = re.compile(r"\b(SEM|SEMESTER|ELECTIVE|GROUP|TOTAL|CREDIT)\b", re.I)


def plausible(title: str, code: str) -> bool:
    """Reject column headers, bare codes and layout debris.

    Multi-column scheme pages interleave unrelated cells on one text line, so a
    candidate that still carries another course's code, a bracketed group tag or
    a structural word is fragments of the layout rather than a course title.
    """
    if not title or len(title) < 4:
        return False
    if title.upper().replace(" ", "") == code.upper():
        return False
    if _NOISE.match(title):
        return False
    if _OTHER_CODE.search(title):          # "Robotics (B3) (URA742) Dynamics"
        return False
    if _STRUCTURAL.search(title):          # "III (SEM (UME746)"
        return False
    if title.count("(") != title.count(")"):
        return False
    letters = sum(ch.isalpha() for ch in title)
    return letters >= 4 and letters / len(title) > 0.5


def from_tables(page, codes: set[str]) -> list[tuple[str, str]]:
    """Find `code` in a table row and take the neighbouring cell as its title."""
    found: list[tuple[str, str]] = []
    try:
        tables = page.extract_tables()
    except Exception:
        return found
    for table in tables or []:
        for row in table or []:
            cells = [clean_title(c) for c in row or []]
            for i, cell in enumerate(cells):
                key = cell.upper().replace(" ", "")
                if key not in codes:
                    continue
                # Prefer the next non-empty cell; else the longest wordy cell.
                nxt = next((c for c in cells[i + 1:] if plausible(c, key)), "")
                if not nxt:
                    others = [c for j, c in enumerate(cells) if j != i and plausible(c, key)]
                    nxt = max(others, key=len) if others else ""
                if nxt:
                    found.append((key, nxt))
    return found


def from_text(text: str, codes: set[str]) -> list[tuple[str, str]]:
    """Fallback for PDFs whose tables do not extract: read the printed line.

    Two layouts occur and they run in opposite directions. Scheme tables print
    ``UME741 Metal Forming 3 0 0 3`` — title after the code. Elective lists print
    ``● Fracture Mechanics (UME740) ● Metal Forming (UME741)`` — title *before*
    the code, so reading forwards there picks up the next entry's name.
    """
    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        compact = line.strip()
        if not compact:
            continue
        upper = compact.upper()
        for code in codes:
            idx = upper.find(code)
            if idx == -1:
                continue

            # "Title (CODE)" — take the bullet/parenthesis-delimited run before it.
            before = compact[:idx].rstrip()
            if before.endswith("("):
                head = before[:-1].strip()
                head = re.split(r"[●•|;]|\s{3,}", head)[-1]
                head = clean_title(head)
                if plausible(head, code):
                    found.append((code, head))
                    continue

            # "CODE : Title" or "CODE Title 3 0 0 3" — read forwards.
            tail = clean_title(compact[idx + len(code):].lstrip(" :.-"))
            tail = re.split(r"[●•]", tail)[0]
            tail = re.sub(r"\([A-Z]{2,5}\d{2,4}[LTP]?\).*$", "", tail).strip()
            if plausible(tail, code):
                found.append((code, tail))
    return found


def scan(pdf_paths: list[Path], codes: set[str]) -> dict[str, Counter]:
    hits: dict[str, Counter] = {code: Counter() for code in codes}
    for path in pdf_paths:
        try:
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    try:
                        text = page.extract_text() or ""
                    except Exception:
                        continue
                    upper = text.upper()
                    present = {c for c in codes if c in upper}
                    if not present:
                        continue
                    for code, title in from_tables(page, present):
                        if code in hits:
                            hits[code][title] += 2      # table rows are stronger evidence
                    for code, title in from_text(text, present):
                        if code in hits:
                            hits[code][title] += 1
        except Exception as exc:                        # unreadable/encrypted PDF
            print(f"  ! could not read {path.name}: {exc}", file=sys.stderr)
    return hits


async def codes_missing_from_catalog() -> Counter:
    """Codes used by live timetables that the catalog cannot name."""
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
    from motor.motor_asyncio import AsyncIOMotorClient
    import os

    from server.curriculum_projection import base_course_code

    client = AsyncIOMotorClient(os.environ["MONGODB_URL"], serverSelectionTimeoutMS=20000)
    db = client[os.environ.get("MONGODB_DB", "mlsc_timetable")]
    catalog = {d["code"] async for d in db.subjects.find({}, {"code": 1})}
    missing: Counter = Counter()
    async for doc in db.timetables.find({}, {"classes": 1}):
        for entry in doc.get("classes") or []:
            options = entry.get("options") or []
            if options:
                for option in options:
                    raw = option.get("subject_code")
                    if not raw:
                        continue
                    code = base_course_code(raw)
                    if code not in catalog and not (option.get("subject_name") or "").strip():
                        missing[code] += 1
                continue
            raw = entry.get("code")
            if not raw:
                continue
            code = base_course_code(raw)
            if code not in catalog and not (entry.get("subject") or "").strip():
                missing[code] += 1
    client.close()
    return missing


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("codes", nargs="*", help="subject codes to look up")
    ap.add_argument("--from-db", action="store_true",
                    help="use the codes the live timetables cannot name")
    ap.add_argument("--pdf-dir", action="append", default=[],
                    help="directory of scheme PDFs (repeatable)")
    ap.add_argument("--pdf", action="append", default=[], help="an individual PDF (repeatable)")
    args = ap.parse_args()

    usage: Counter = Counter()
    codes = {c.strip().upper() for c in args.codes if c.strip()}
    if args.from_db:
        usage = asyncio.run(codes_missing_from_catalog())
        codes |= set(usage)
    if not codes:
        ap.error("give some codes, or --from-db")

    paths: list[Path] = []
    for d in args.pdf_dir or [REPO_ROOT.parent / "Schema"]:
        paths.extend(sorted(Path(d).glob("*.pdf")) + sorted(Path(d).glob("*.PDF")))
    paths.extend(Path(p) for p in args.pdf)
    paths = [p for p in paths if p.is_file()]
    if not paths:
        ap.error("no PDFs found")

    print(f"Searching {len(paths)} PDF(s) for {len(codes)} code(s)…\n")
    hits = scan(paths, codes)

    order = sorted(codes, key=lambda c: (-usage.get(c, 0), c))
    width = max(len(c) for c in order)
    resolved = 0
    for code in order:
        best = hits[code].most_common(1)
        seen = f"×{usage[code]}" if usage.get(code) else ""
        if best:
            resolved += 1
            title, score = best[0]
            alts = [t for t, _ in hits[code].most_common(3)[1:]]
            print(f"  {code:<{width}} {seen:>6}  {title}")
            if alts:
                print(f"  {'':<{width}} {'':>6}  alt: {'; '.join(alts)}")
        else:
            print(f"  {code:<{width}} {seen:>6}  -- not found in any scheme PDF --")
    print(f"\n{resolved}/{len(order)} resolved.")
    if resolved:
        print("\nPaste-ready for the admin catalog (code<TAB>title):")
        for code in order:
            best = hits[code].most_common(1)
            if best:
                print(f"{code}\t{best[0][0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
