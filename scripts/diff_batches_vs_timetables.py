"""Compare `batches` and `timetables` collections to explain the count mismatch."""
from __future__ import annotations

import asyncio
import os
import re
import sys
from collections import Counter

from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("MONGODB_DB", "mlsc_timetable")

_BATCH_PATTERN = re.compile(r"^(?P<year>\d)(?P<section>[A-Z]+)")


def _derive_year(code: str) -> int | None:
    m = _BATCH_PATTERN.match(code.upper())
    if not m:
        return None
    try:
        return int(m.group("year"))
    except ValueError:
        return None


def _section(code: str) -> str | None:
    m = _BATCH_PATTERN.match(code.upper())
    return m.group("section") if m else None


async def main() -> None:
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    batch_codes = {d["code"] async for d in db.batches.find({}, {"code": 1, "_id": 0})}
    tt_codes = {d["code"] async for d in db.timetables.find({}, {"code": 1, "_id": 0})}

    print(f"batches    : {len(batch_codes)}")
    print(f"timetables : {len(tt_codes)}")
    print()

    orphan_tts = sorted(tt_codes - batch_codes)
    orphan_batches = sorted(batch_codes - tt_codes)

    print(f"in timetables but NOT in batches : {len(orphan_tts)}")
    print(f"in batches but NOT in timetables : {len(orphan_batches)}")
    print()

    if orphan_tts:
        print("=== timetables-only codes (first 60) ===")
        for c in orphan_tts[:60]:
            print(f"  {c}")
        if len(orphan_tts) > 60:
            print(f"  ... and {len(orphan_tts) - 60} more")
        print()

        # bucket by year + section prefix to find a pattern
        by_year = Counter(_derive_year(c) for c in orphan_tts)
        by_section = Counter(_section(c) for c in orphan_tts)
        print("orphan-timetables grouped by leading year digit:")
        for y, n in sorted(by_year.items(), key=lambda x: (x[0] is None, x[0])):
            print(f"  year {y!r:>5} : {n}")
        print()
        print("orphan-timetables grouped by section prefix (top 15):")
        for sec, n in by_section.most_common(15):
            print(f"  {sec!r:>10} : {n}")
        print()

    if orphan_batches:
        print("=== batches-only codes (first 30) ===")
        for c in orphan_batches[:30]:
            print(f"  {c}")
        if len(orphan_batches) > 30:
            print(f"  ... and {len(orphan_batches) - 30} more")
        print()

    # full-collection year histograms for context
    tt_by_year = Counter(_derive_year(c) for c in tt_codes)
    b_by_year = Counter(_derive_year(c) for c in batch_codes)
    print("=== year histogram (timetables vs batches) ===")
    all_years = sorted(set(tt_by_year) | set(b_by_year), key=lambda x: (x is None, x))
    print(f"{'year':>6} {'timetables':>12} {'batches':>10} {'delta':>8}")
    for y in all_years:
        t = tt_by_year.get(y, 0)
        b = b_by_year.get(y, 0)
        print(f"{str(y):>6} {t:>12} {b:>10} {t - b:>+8}")

    client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
