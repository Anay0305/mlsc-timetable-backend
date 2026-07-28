#!/usr/bin/env python3
"""Migrate legacy slot-key overrides into the V2 customization collection.

The script defaults to localhost and dry-run. A non-local write requires both
``--apply`` and ``--allow-nonlocal-write`` so production cannot be modified by
accident. On apply, the complete legacy collection is copied to a backup
collection before any V2 documents are inserted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pymongo import ASCENDING, MongoClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from server.personal_timetable import convert_legacy_entries  # noqa: E402


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mongo-url", default="mongodb://127.0.0.1:27017")
    parser.add_argument("--database", default="mlsc_timetable_migration_test")
    parser.add_argument("--legacy-collection", default="overrides")
    parser.add_argument("--target-collection", default="personal_timetable_customizations_v2")
    parser.add_argument("--backup-collection", default="overrides_legacy_backup_v2")
    parser.add_argument("--apply", action="store_true", help="write backup and V2 documents")
    parser.add_argument("--allow-nonlocal-write", action="store_true")
    parser.add_argument(
        "--expected-legacy-documents",
        type=int,
        help="required safety check for non-local writes; must match the live source count",
    )
    return parser.parse_args()


def is_local_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in LOCAL_HOSTS and parsed.scheme == "mongodb"


def datetime_sort_key(value: Any) -> float:
    if not isinstance(value, datetime):
        return float("-inf")
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.timestamp()


def merge_legacy_group(docs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entries: dict[str, Any] = {}
    sources: dict[str, tuple[float, str, str]] = {}
    conflicts: list[dict[str, Any]] = []
    for doc in sorted(docs, key=lambda item: (datetime_sort_key(item.get("updated_at")), str(item.get("_id")))):
        updated = doc.get("updated_at")
        doc_id = str(doc.get("_id"))
        for slot, value in (doc.get("entries") or {}).items():
            canonical = json.dumps(value, sort_keys=True, default=str)
            if slot in entries:
                previous = sources[slot]
                if previous[2] != canonical:
                    conflicts.append({
                        "type": "duplicate_slot_conflict",
                        "slot": slot,
                        "previous_document_id": previous[1],
                        "selected_document_id": doc_id,
                    })
            migrated = dict(value)
            migrated["migration"] = {
                "legacy_document_id": doc_id,
                "legacy_document_updated_at": updated,
            }
            entries[slot] = migrated
            sources[slot] = (datetime_sort_key(updated), doc_id, canonical)
    return entries, conflicts


def main() -> int:
    args = parse_args()
    if args.apply and not is_local_url(args.mongo_url) and not args.allow_nonlocal_write:
        print("REFUSED: non-local writes require --allow-nonlocal-write", file=sys.stderr)
        return 2
    if args.apply and not is_local_url(args.mongo_url) and args.expected_legacy_documents is None:
        print("REFUSED: non-local writes require --expected-legacy-documents", file=sys.stderr)
        return 2

    client = MongoClient(args.mongo_url, serverSelectionTimeoutMS=10000, uuidRepresentation="standard")
    client.admin.command("ping")
    db = client[args.database]
    legacy = db[args.legacy_collection]
    target = db[args.target_collection]
    backup = db[args.backup_collection]
    docs = list(legacy.find({}))
    if args.expected_legacy_documents is not None and len(docs) != args.expected_legacy_documents:
        print(
            f"REFUSED: expected {args.expected_legacy_documents} legacy documents, found {len(docs)}",
            file=sys.stderr,
        )
        client.close()
        return 4
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for doc in docs:
        groups[(str(doc.get("user_id") or ""), str(doc.get("batch") or ""))].append(doc)

    output_docs: list[dict[str, Any]] = []
    conflict_counts: Counter[str] = Counter()
    physical_entries = sum(len(doc.get("entries") or {}) for doc in docs)
    logical_entries = 0
    migrated_operations = 0
    now = datetime.now(timezone.utc)

    for (user_id, batch), group_docs in sorted(groups.items()):
        merged_entries, duplicate_conflicts = merge_legacy_group(group_docs)
        timetable = db["timetables"].find_one({"code": batch}) or {"classes": []}
        operations, mapping_conflicts = convert_legacy_entries(
            user_id=user_id,
            batch=batch,
            canonical_classes=timetable.get("classes") or [],
            entries=merged_entries,
            migrated_at=now,
        )
        all_conflicts = duplicate_conflicts + mapping_conflicts
        conflict_counts.update(item["type"] for item in all_conflicts)
        logical_entries += len(merged_entries)
        migrated_operations += len(operations)
        output_docs.append({
            "user_id": user_id,
            "batch": batch,
            "revision": 1,
            "operations": operations,
            "created_at": now,
            "updated_at": now,
        })

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "database": args.database,
        "legacy_documents": len(docs),
        "user_batch_groups": len(groups),
        "physical_entries": physical_entries,
        "logical_entries": logical_entries,
        "migrated_operations": migrated_operations,
        "conflicts": dict(sorted(conflict_counts.items())),
    }

    if args.apply:
        if backup.count_documents({}) or target.count_documents({}):
            print("REFUSED: backup or target collection is not empty", file=sys.stderr)
            client.close()
            return 3
        if docs:
            backup.insert_many(docs, ordered=True)
        if output_docs:
            target.insert_many(output_docs, ordered=True)
        target.create_index(
            [("user_id", ASCENDING), ("batch", ASCENDING)],
            unique=True,
            name="unique_user_batch_v2",
        )
        report["backup_documents"] = backup.count_documents({})
        report["written_v2_documents"] = target.count_documents({})
        report["written_v2_operations"] = sum(
            len(doc.get("operations") or {}) for doc in target.find({}, {"operations": 1})
        )

    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
