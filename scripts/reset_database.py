#!/usr/bin/env python3
"""Delete MongoDB collections for a fresh timetable reset.

Default mode removes timetable and ingest state while preserving the subject
catalog, admin access, users, and content. Use ``--all`` only when the entire
application database should be emptied.

Examples:
    python scripts/reset_database.py --dry-run
    python scripts/reset_database.py --yes
    python scripts/reset_database.py --all --yes

The production ``MONGODB_URL`` and ``MONGODB_DB`` must be available in the
environment. The backend-local ``.env`` is loaded automatically by config.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from server.db import close_db, init_db
from server.db.models import (
    ALL_DOCUMENTS,
    BatchDoc,
    ChangeRequestDoc,
    IngestSnapshotDoc,
    OverrideDoc,
    ParsingErrorDoc,
    SemesterDoc,
    TimetableDoc,
    UploadAttemptDoc,
)


FRESH_TIMETABLE_COLLECTIONS = (
    BatchDoc,
    TimetableDoc,
    SemesterDoc,
    UploadAttemptDoc,
    IngestSnapshotDoc,
    ParsingErrorDoc,
    ChangeRequestDoc,
    OverrideDoc,
)

ALL_COLLECTIONS = tuple(ALL_DOCUMENTS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete every application collection, including subjects, users, and admins",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the destructive confirmation prompt",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List collections without connecting or deleting anything",
    )
    return parser.parse_args()


def selected_documents(delete_all: bool) -> tuple[type, ...]:
    return ALL_COLLECTIONS if delete_all else FRESH_TIMETABLE_COLLECTIONS


async def reset(delete_all: bool) -> list[dict[str, int | str]]:
    await init_db()
    try:
        results: list[dict[str, int | str]] = []
        for document in selected_documents(delete_all):
            collection = document.get_motor_collection()
            result = await collection.delete_many({})
            results.append({"collection": collection.name, "deleted": result.deleted_count})
        return results
    finally:
        await close_db()


def main() -> None:
    args = parse_args()
    documents = selected_documents(args.all)
    names = [document.Settings.name for document in documents]

    print(json.dumps({"mode": "all" if args.all else "fresh_timetable", "collections": names}, indent=2))
    if args.dry_run:
        return
    if not args.yes:
        answer = input("Type RESET to permanently delete these collections: ").strip()
        if answer != "RESET":
            raise SystemExit("Aborted")

    print(json.dumps(asyncio.run(reset(args.all)), indent=2))


if __name__ == "__main__":
    main()
