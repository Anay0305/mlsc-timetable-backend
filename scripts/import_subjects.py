#!/usr/bin/env python3
"""Import a ``{subject_code: subject_name}`` JSON file into MongoDB.

This uses the same normalization as the Admin Fix catalog flow: codes are
upper-cased and trailing L/T/P suffixes are removed. Existing rows are left
unchanged by default so admin corrections are not overwritten accidentally.

Examples:
    python scripts/import_subjects.py --file /path/to/subjects.json --dry-run
    python scripts/import_subjects.py --file /path/to/subjects.json
    python scripts/import_subjects.py --file /path/to/subjects.json --overwrite

The production ``MONGODB_URL`` and ``MONGODB_DB`` must be available in the
environment. The backend-local ``.env`` is loaded automatically by config.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from server import storage
from server.db import close_db, init_db
from server.db.models import SubjectDoc

logger = logging.getLogger("import_subjects")


def parse_args() -> argparse.Namespace:
    default_file = Path(__file__).resolve().parents[2] / "subjects.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--file",
        type=Path,
        default=default_file,
        help=f"JSON mapping of subject code to name (default: {default_file})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace names of existing catalog rows; default is insert-missing-only",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report changes without writing to MongoDB",
    )
    return parser.parse_args()


def load_subjects(path: Path) -> list[tuple[str, str]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("input JSON must be an object mapping subject code to name")

    rows: list[tuple[str, str]] = []
    for raw_code, raw_name in payload.items():
        if not isinstance(raw_code, str) or not isinstance(raw_name, str):
            raise ValueError("every subject code and name must be a string")
        code = storage._normalize_subject_code(raw_code)
        name = raw_name.strip()
        if not name:
            raise ValueError(f"subject {raw_code!r} has an empty name")
        rows.append((code, name))
    return rows


async def run(args: argparse.Namespace) -> dict[str, int]:
    rows = load_subjects(args.file)
    stats = {"input": len(rows), "added": 0, "updated": 0, "skipped": 0, "failed": 0}
    seen: set[str] = set()

    if not args.dry_run:
        await init_db()

    try:
        for code, name in rows:
            if code in seen:
                stats["skipped"] += 1
                continue
            seen.add(code)

            existing = None if args.dry_run else await SubjectDoc.find_one(SubjectDoc.code == code)
            if existing is not None and not args.overwrite:
                stats["skipped"] += 1
                continue

            if args.dry_run:
                stats["updated" if existing else "added"] += 1
                continue

            try:
                if existing is None:
                    await SubjectDoc(
                        code=code,
                        name=name,
                        source="import",
                        created_by="import_subjects.py",
                    ).insert()
                    stats["added"] += 1
                else:
                    await existing.set({
                        "name": name,
                        "source": "import",
                    })
                    stats["updated"] += 1
            except Exception:
                logger.exception("failed to import %s", code)
                stats["failed"] += 1

        if not args.dry_run:
            from timetable_parser.core.subject_catalog import invalidate_catalog
            invalidate_catalog()
    finally:
        if not args.dry_run:
            await close_db()

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args()
    stats = asyncio.run(run(args))
    print(json.dumps(stats, sort_keys=True))


if __name__ == "__main__":
    main()
