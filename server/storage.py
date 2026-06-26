"""MongoDB-backed data store (seam for the HTTP layer).

All routers go through this module. Beanie documents live in
:mod:`server.db.models`; this file exposes async helpers that return plain
dicts/lists in the exact shape the frontend expects, so the API contract is
independent of the underlying ODM.

Optional `JSON_MIRROR=1` toggles disk snapshots into ``data/`` for audit /
git-tracking — same layout the previous JSON store used.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.config import Settings, get_settings
from server.db.models import BaselineDoc, BatchDoc, ContributorDoc, SemesterDoc, TimetableDoc

logger = logging.getLogger(__name__)


class BatchNotFound(Exception):
    def __init__(self, batch: str) -> None:
        super().__init__(f"Batch not found: {batch}")
        self.batch = batch


class DataMissing(Exception):
    """Raised when expected baseline data (semester label, batch list) is absent."""


# ── Reads ────────────────────────────────────────────────────────────────
async def read_batch_list(settings: Settings | None = None) -> list[str]:
    codes = [doc.code async for doc in BatchDoc.find_all(sort=[("code", 1)])]
    if not codes:
        # Fall back to whatever timetables exist, so the API isn't empty before
        # the BatchDoc collection has been seeded.
        codes = sorted({doc.code async for doc in TimetableDoc.find_all()})
    return codes


async def read_current(settings: Settings | None = None) -> dict[str, Any]:
    doc = await SemesterDoc.find_one(SemesterDoc.key == "current")
    if doc is None:
        raise DataMissing("no current semester set (PUT /admin/current)")
    return {"label": doc.label}


async def read_timetable(batch: str, settings: Settings | None = None) -> dict[str, Any]:
    code = _safe_batch(batch)
    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        raise BatchNotFound(batch)
    return _timetable_payload(doc)


# ── Writes ───────────────────────────────────────────────────────────────
async def write_batch_list(
    batches: list[str],
    settings: Settings | None = None,
    *,
    sheet_by_code: dict[str, str] | None = None,
) -> None:
    """Replace the batch directory.

    Adds new codes, updates `source_sheet` on existing ones, and removes codes
    no longer present in `batches`.
    """
    settings = settings or get_settings()
    codes = sorted({str(b) for b in batches})
    sheet_by_code = sheet_by_code or {}
    now = datetime.now(timezone.utc)

    existing = {doc.code: doc async for doc in BatchDoc.find_all()}
    for code in codes:
        meta = _derive_batch_meta(code)
        doc = existing.pop(code, None)
        if doc is None:
            await BatchDoc(
                code=code,
                year=meta["year"],
                section=meta["section"],
                source_sheet=sheet_by_code.get(code),
            ).insert()
        else:
            updates: dict[str, Any] = {"updated_at": now}
            if sheet_by_code.get(code) and doc.source_sheet != sheet_by_code.get(code):
                updates["source_sheet"] = sheet_by_code.get(code)
            if meta["year"] != doc.year:
                updates["year"] = meta["year"]
            if meta["section"] != doc.section:
                updates["section"] = meta["section"]
            await doc.set(updates)
    for stale in existing.values():
        await stale.delete()

    if settings.json_mirror:
        _mirror_json(settings.data_dir / "batch.json", codes)


async def write_current(payload: dict[str, Any], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    label = payload.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("payload must include a non-empty 'label' string")
    doc = await SemesterDoc.find_one(SemesterDoc.key == "current")
    if doc is None:
        await SemesterDoc(key="current", label=label).insert()
    else:
        await doc.set({"label": label, "updated_at": datetime.now(timezone.utc)})

    if settings.json_mirror:
        _mirror_json(settings.data_dir / "current.json", {"label": label})


async def write_timetable(
    batch: str,
    payload: dict[str, Any],
    settings: Settings | None = None,
    *,
    source_sheet: str | None = None,
    source_file: str | None = None,
) -> None:
    settings = settings or get_settings()
    code = _safe_batch(batch)
    semester_obj = payload.get("semester") or {}
    semester_label = (
        semester_obj.get("label") if isinstance(semester_obj, dict) else None
    )
    if not isinstance(semester_label, str) or not semester_label.strip():
        raise ValueError("payload.semester.label must be a non-empty string")
    classes = payload.get("classes") or []
    if not isinstance(classes, list):
        raise ValueError("payload.classes must be a list")

    source = {
        "sheet": source_sheet,
        "file": source_file,
        "ingested_at": datetime.now(timezone.utc),
    }

    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        await TimetableDoc(
            code=code,
            semester=semester_label,
            classes=classes,
            source=source,
        ).insert()
    else:
        await doc.set({
            "semester": semester_label,
            "classes": classes,
            "source": source,
            "updated_at": datetime.now(timezone.utc),
        })

    if settings.json_mirror:
        path = settings.data_dir / "timetable" / f"{code}.json"
        _mirror_json(path, _timetable_payload_from_raw(code, semester_label, classes))


async def delete_timetable(batch: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    code = _safe_batch(batch)
    doc = await TimetableDoc.find_one(TimetableDoc.code == code)
    if doc is None:
        return False
    await doc.delete()
    if settings.json_mirror:
        path = settings.data_dir / "timetable" / f"{code}.json"
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    return True


# ── Baselines ────────────────────────────────────────────────────────────
def semester_prefix(label: str | None) -> str:
    """Derive an ``E``/``O`` prefix from a semester label like ``"EVEN 25-26"``."""
    if not label:
        return "?"
    s = label.strip().upper()
    if s.startswith("EVEN") or s.startswith("E "):
        return "E"
    if s.startswith("ODD") or s.startswith("O "):
        return "O"
    return s[:1] if s else "?"


_BASELINE_KEY = re.compile(r"^[EO][1-9][A-Z]$")


def _safe_baseline_key(key: str) -> str:
    cleaned = "".join(ch for ch in (key or "").strip().upper() if ch.isalnum())
    if not _BASELINE_KEY.match(cleaned):
        raise ValueError(
            f"invalid baseline key {key!r}: expected '{{E|O}}{{year}}{{stream}}' e.g. 'E1A'"
        )
    return cleaned


def _baseline_payload(doc: BaselineDoc) -> dict[str, Any]:
    counts = dict(doc.counts)
    total = sum(int(v) for v in counts.values() if isinstance(v, int))
    return {
        "key": doc.key,
        "semester_prefix": doc.semester_prefix,
        "group": doc.group,
        "counts": counts,
        "total": total,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


async def list_baselines(settings: Settings | None = None) -> list[dict[str, Any]]:
    return [_baseline_payload(doc) async for doc in BaselineDoc.find_all(sort=[("key", 1)])]


async def read_baseline(key: str, settings: Settings | None = None) -> dict[str, Any]:
    cleaned = _safe_baseline_key(key)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned)
    if doc is None:
        raise DataMissing(f"no baseline for {cleaned}")
    return _baseline_payload(doc)


async def write_baseline(
    key: str,
    counts: dict[str, int],
    settings: Settings | None = None,
) -> dict[str, Any]:
    cleaned_key = _safe_baseline_key(key)
    if not isinstance(counts, dict) or not counts:
        raise ValueError("counts must be a non-empty mapping of type → integer")
    cleaned_counts: dict[str, int] = {}
    for type_name, value in counts.items():
        if not isinstance(type_name, str) or not type_name.strip():
            raise ValueError(f"invalid type name: {type_name!r}")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"counts[{type_name!r}] must be a non-negative int, got {value!r}")
        cleaned_counts[type_name.strip()] = value
    if cleaned_key in {"total"}:  # paranoid: never let a reserved key sneak in
        raise ValueError("'total' is a reserved key")
    if "total" in cleaned_counts:
        raise ValueError("'total' is reserved; it is derived from the sum of the other counts")

    prefix = cleaned_key[0]
    group = cleaned_key[1:]
    now = datetime.now(timezone.utc)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    if doc is None:
        doc = BaselineDoc(
            key=cleaned_key,
            semester_prefix=prefix,
            group=group,
            counts=cleaned_counts,
        )
        await doc.insert()
    else:
        await doc.set({"counts": cleaned_counts, "updated_at": now})
        doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned_key)
    return _baseline_payload(doc)


async def delete_baseline(key: str, settings: Settings | None = None) -> bool:
    cleaned = _safe_baseline_key(key)
    doc = await BaselineDoc.find_one(BaselineDoc.key == cleaned)
    if doc is None:
        return False
    await doc.delete()
    return True


async def read_baselines_for_prefix(
    prefix: str,
    settings: Settings | None = None,
) -> dict[str, dict[str, int]]:
    """Return ``{group: counts}`` for the given semester prefix (e.g. ``"E"``)."""
    prefix = (prefix or "").strip().upper()[:1]
    if not prefix:
        return {}
    out: dict[str, dict[str, int]] = {}
    async for doc in BaselineDoc.find(BaselineDoc.semester_prefix == prefix):
        out[doc.group] = dict(doc.counts)
    return out


# ── Contributors ─────────────────────────────────────────────────────────
_GITHUB_USERNAME = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,38})$")


def _safe_username(username: str) -> str:
    cleaned = (username or "").strip().lstrip("@")
    if not _GITHUB_USERNAME.match(cleaned):
        raise ValueError(
            f"invalid GitHub username {username!r}: alphanumeric + hyphen, 1-39 chars, "
            "cannot start with a hyphen"
        )
    if cleaned.endswith("-") or "--" in cleaned:
        raise ValueError(f"invalid GitHub username {username!r}")
    return cleaned


async def list_contributors(settings: Settings | None = None) -> list[dict[str, Any]]:
    return [
        {
            "username": doc.username,
            "display_name": doc.display_name,
            "created_at": doc.created_at.isoformat() if doc.created_at else None,
        }
        async for doc in ContributorDoc.find_all(sort=[("username", 1)])
    ]


async def list_contributor_usernames(settings: Settings | None = None) -> list[str]:
    return [doc.username async for doc in ContributorDoc.find_all(sort=[("username", 1)])]


async def add_contributor(
    username: str,
    display_name: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    cleaned = _safe_username(username)
    existing = await ContributorDoc.find_one(ContributorDoc.username == cleaned)
    if existing is not None:
        updates: dict[str, Any] = {}
        if display_name is not None and display_name != existing.display_name:
            updates["display_name"] = display_name or None
        if updates:
            await existing.set(updates)
            existing = await ContributorDoc.find_one(ContributorDoc.username == cleaned)
        return {
            "username": existing.username,
            "display_name": existing.display_name,
            "created_at": existing.created_at.isoformat() if existing.created_at else None,
        }
    doc = ContributorDoc(username=cleaned, display_name=display_name or None)
    await doc.insert()
    return {
        "username": doc.username,
        "display_name": doc.display_name,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }


async def delete_contributor(username: str, settings: Settings | None = None) -> bool:
    cleaned = _safe_username(username)
    doc = await ContributorDoc.find_one(ContributorDoc.username == cleaned)
    if doc is None:
        return False
    await doc.delete()
    return True


# ── Misc ─────────────────────────────────────────────────────────────────
def maybe_git_commit(message: str, settings: Settings | None = None) -> None:
    """Commit the JSON mirror directory; no-op unless both git-auto-commit
    and JSON_MIRROR are enabled."""
    settings = settings or get_settings()
    if not settings.git_auto_commit or not settings.json_mirror:
        return
    try:
        subprocess.run(["git", "add", str(settings.data_dir)], check=True, cwd=settings.data_dir.parent)
        subprocess.run(
            ["git", "commit", "-m", message, "--", str(settings.data_dir)],
            check=True,
            cwd=settings.data_dir.parent,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        logger.warning("git auto-commit skipped: %s", exc)


# ── Internals ────────────────────────────────────────────────────────────
def _timetable_payload(doc: TimetableDoc) -> dict[str, Any]:
    return {
        "batch": doc.code,
        "semester": {"label": doc.semester},
        "classes": [_serialize_class(c) for c in doc.classes],
    }


def _timetable_payload_from_raw(code: str, semester_label: str, classes: list[Any]) -> dict[str, Any]:
    return {
        "batch": code,
        "semester": {"label": semester_label},
        "classes": [_serialize_class(c) for c in classes],
    }


def _serialize_class(entry: Any) -> dict[str, Any]:
    """Coerce a ClassEntry or plain dict to a stable JSON dict."""
    if hasattr(entry, "model_dump"):
        return entry.model_dump(exclude_none=False)
    return dict(entry)


def _safe_batch(batch: str) -> str:
    cleaned = "".join(ch for ch in batch.strip().upper() if ch.isalnum())
    if not cleaned:
        raise BatchNotFound(batch)
    return cleaned


_BATCH_PATTERN = re.compile(r"^(?P<year>\d)(?P<section>[A-Z]+)")


def _derive_batch_meta(code: str) -> dict[str, Any]:
    """Best-effort split of a batch code like "1B11" into year=1, section="B"."""
    m = _BATCH_PATTERN.match(code)
    if not m:
        return {"year": None, "section": None}
    year = int(m.group("year"))
    return {"year": year if 1 <= year <= 9 else None, "section": m.group("section")}


def _mirror_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{int(time.time() * 1000)}")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unserialisable: {type(value).__name__}")
