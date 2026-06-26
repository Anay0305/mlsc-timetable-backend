"""Console script: `mlsc-timetable build|migrate-json`.

`build` parses an .xlsx and upserts everything into MongoDB (the same code path
`POST /admin/ingest` uses).

`migrate-json` reads the legacy on-disk JSON files (`data/batch.json`,
`data/current.json`, `data/timetable/*.json`) and pushes them into Mongo. Handy
one-shot when migrating from the file-only backend.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path

from server.config import Settings, get_settings
from server.db import close_db, init_db
from server.ingest import parse_workbook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mlsc-timetable")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="Parse a timetable .xlsx and upsert into Mongo.")
    build.add_argument("xlsx", type=Path, help="Path to the timetable spreadsheet.")
    build.add_argument(
        "--semester",
        required=True,
        help="Semester label written to the `semester` collection (e.g. 'ODD SEM 26-27').",
    )
    build.add_argument(
        "--sheet",
        default="all",
        help=(
            "Which worksheet(s) to parse: 'all' (default), 'active', an exact "
            "title, '@<index>', or an fnmatch-style glob like '1ST YEAR *'."
        ),
    )
    build.add_argument(
        "--mirror-json",
        action="store_true",
        help="Also write JSON snapshots into data/ (sets JSON_MIRROR for this run).",
    )
    build.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Mirror output directory (only used with --mirror-json; defaults to ./data).",
    )
    build.add_argument(
        "--git-commit",
        action="store_true",
        help="Auto git-commit the mirror data/ folder after writing (requires --mirror-json).",
    )

    migrate = subparsers.add_parser(
        "migrate-json",
        help="Import legacy data/*.json files into Mongo (idempotent).",
    )
    migrate.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        default=Path("data"),
        help="Directory containing batch.json, current.json, timetable/*.json (default: ./data).",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help=(
            "Sanity-check that every batch within the same {YEAR}{ALPHA} group "
            "(e.g. all 1A**) has the same per-type class breakdown."
        ),
    )
    doctor.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        default=Path("data"),
        help="Directory containing timetable/*.json (default: ./data).",
    )
    doctor.add_argument(
        "--semester",
        default=None,
        help=(
            "Semester label to look up baselines for (e.g. 'EVEN 25-26'). "
            "Defaults to the value stored in data/current.json."
        ),
    )
    doctor.add_argument(
        "--no-db",
        action="store_true",
        help="Skip Mongo; fall back to per-type mode for the 'expected' breakdown.",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.command == "build":
        return asyncio.run(_run_build(args))
    if args.command == "migrate-json":
        return asyncio.run(_run_migrate_json(args))
    if args.command == "doctor":
        return asyncio.run(_run_doctor(args))
    return 2


async def _run_build(args: argparse.Namespace) -> int:
    settings = _settings_for_cli(
        mirror=args.mirror_json,
        mirror_dir=args.out,
        git_commit=args.git_commit,
    )
    await init_db(settings)
    try:
        summary = await parse_workbook(
            args.xlsx,
            semester_label=args.semester,
            settings=settings,
            sheet=args.sheet,
        )
    finally:
        await close_db()

    print(
        f"wrote {summary['batches']} batches / {summary['classes']} classes "
        f"from sheets={summary['sheets_used']}",
        file=sys.stderr,
    )
    if summary["multi_sheet_batches"]:
        print(
            f"  multi-sheet batches (merged + deduped): {summary['multi_sheet_batches']}",
            file=sys.stderr,
        )
    doctor = summary.get("doctor")
    if doctor:
        print(
            f"  doctor: {doctor['consistent_groups']}/{doctor['total_groups']} "
            f"groups consistent, {doctor['mismatched_groups']} mismatched",
            file=sys.stderr,
        )
    return 0


async def _run_migrate_json(args: argparse.Namespace) -> int:
    from server import storage  # local import: needs Beanie initialised first

    in_dir = args.in_dir.resolve()
    batch_path = in_dir / "batch.json"
    current_path = in_dir / "current.json"
    timetable_dir = in_dir / "timetable"
    if not in_dir.exists():
        print(f"error: input dir not found: {in_dir}", file=sys.stderr)
        return 1

    settings = _settings_for_cli()
    await init_db(settings)
    try:
        if current_path.exists():
            await storage.write_current(_load_json(current_path), settings=settings)
            print(f"current  ← {current_path}", file=sys.stderr)

        batches: list[str] = []
        if batch_path.exists():
            batches = list(_load_json(batch_path))
            await storage.write_batch_list(batches, settings=settings)
            print(f"batches  ← {batch_path} ({len(batches)} codes)", file=sys.stderr)

        wrote = 0
        if timetable_dir.exists():
            for path in sorted(timetable_dir.glob("*.json")):
                payload = _load_json(path)
                code = payload.get("batch") or path.stem
                await storage.write_timetable(code, payload, settings=settings, source_file=path.name)
                wrote += 1
            print(f"timetables ← {timetable_dir} ({wrote} files)", file=sys.stderr)
    finally:
        await close_db()

    return 0


async def _run_doctor(args: argparse.Namespace) -> int:
    """Group timetables by {YEAR}{ALPHA} and report per-type mismatches."""
    from server import storage
    from server.doctor import doctor_report_from_dir, format_doctor_report

    in_dir = args.in_dir.resolve()
    timetable_dir = in_dir / "timetable"
    if not timetable_dir.exists():
        print(f"error: timetable dir not found: {timetable_dir}", file=sys.stderr)
        return 1

    baselines: dict[str, dict[str, int]] = {}
    prefix: str | None = None
    if not args.no_db:
        settings = _settings_for_cli()
        await init_db(settings)
        try:
            label = args.semester
            if label is None:
                try:
                    label = (await storage.read_current(settings=settings)).get("label")
                except storage.DataMissing:
                    label = None
            if label:
                prefix = storage.semester_prefix(label)
                baselines = await storage.read_baselines_for_prefix(prefix, settings=settings)
                print(
                    f"using baselines for semester={label!r} (prefix={prefix}): "
                    f"{len(baselines)} group(s) configured",
                    file=sys.stderr,
                )
            else:
                print(
                    "no semester label found; falling back to mode-based expected counts",
                    file=sys.stderr,
                )
        finally:
            await close_db()

    report = doctor_report_from_dir(
        timetable_dir,
        baselines_by_group=baselines or None,
        semester_prefix=prefix,
    )
    print(format_doctor_report(report))
    return 0 if report["mismatched_groups"] == 0 else 1


def _settings_for_cli(
    *,
    mirror: bool = False,
    mirror_dir: Path | None = None,
    git_commit: bool = False,
) -> Settings:
    base = get_settings()
    overrides: dict[str, object] = {}
    if mirror:
        overrides["json_mirror"] = True
        if mirror_dir is not None:
            overrides["data_dir"] = mirror_dir.resolve()
    if git_commit or _truthy(os.environ.get("GIT_AUTO_COMMIT")):
        overrides["git_auto_commit"] = True
    return replace(base, **overrides) if overrides else base


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
