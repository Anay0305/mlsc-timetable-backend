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
from server.db.models import (
    AdminEmailDoc,
    AnnouncementDoc,
    BaselineDoc,
    BatchDoc,
    ChangeRequestDoc,
    ContributorDoc,
    ExamDateDoc,
    SemesterDoc,
    TimetableDoc,
    UploadAttemptDoc,
    UploadErrorRow,
)

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


# ── Overrides ──────────────────────────────────────────────────────
_BATCH_CODE = re.compile(r"^[A-Z0-9]{2,16}$")
_DAY_PATTERN = re.compile(r"^[A-Za-z]{3,12}$")
# Reasonable upper bound; storage layer rejects floods even if the HTTP rate
# limiter is bypassed/misconfigured. Keep tight — admin-reviewed queue.
MAX_PENDING_PER_REQUESTER = 20
MAX_PENDING_PER_BATCH = 100
MAX_PENDING_TOTAL = 1000
# Reject identical pending requests (same batch+slot+kind+entry signature) so
# a refresh-bug or accidental double-submit doesn't fan out the queue.


class ChangeRequestRefused(Exception):
    """Storage refused an override (validation, quota, duplicate)."""

    def __init__(self, message: str, code: str = "refused") -> None:
        super().__init__(message)
        self.code = code


def _safe_class_prefix(batch_code: str) -> str:
    """First-3-char prefix of a batch (e.g. ``"1B12"`` -> ``"1B1"``)."""
    cleaned = _safe_batch(batch_code)
    if len(cleaned) < 3:
        raise ChangeRequestRefused(
            f"batch {batch_code!r} is too short to derive a class prefix",
            code="bad_batch",
        )
    return cleaned[:3]


def _resolve_scope_batches(scope: str, requester_batch: str) -> list[str]:
    """Return the list of canonical batches an approved request should touch."""
    requester = _safe_batch(requester_batch)
    if scope == "batch":
        return [requester]
    if scope == "class":
        prefix = _safe_class_prefix(requester)
        return [prefix]  # placeholder; resolved against actual batches in approval
    raise ChangeRequestRefused(f"unknown scope {scope!r}", code="bad_scope")


def _serialize_change_request(doc) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(doc.id),
        "requester_id": doc.requester_id,
        "requester_batch": doc.requester_batch,
        "semester": doc.semester,
        "scope": doc.scope,
        "kind": doc.kind,
        "day": doc.day,
        "start_time": doc.start_time,
        "entry": _serialize_class(doc.entry) if doc.entry is not None else None,
        "status": doc.status,
        "decision_note": doc.decision_note,
        "decided_by": doc.decided_by,
        "decided_at": doc.decided_at.isoformat() if doc.decided_at else None,
        "applied_batches": list(doc.applied_batches or []),
        "created_at": doc.created_at.isoformat(),
    }
    return payload


async def create_change_request(
    *,
    requester_batch: str,
    scope: str,
    kind: str,
    day: str,
    start_time: str,
    entry: dict[str, Any] | None = None,
    requester_id: str | None = None,
) -> dict[str, Any]:
    """Validate + insert a pending ChangeRequestDoc.

    Raises ChangeRequestRefused with a stable ``code`` on quota / dupe / shape
    problems so the HTTP layer can map it to 400 / 409 cleanly.
    """
    if kind not in {"add", "edit", "delete"}:
        raise ChangeRequestRefused(f"unknown kind {kind!r}", code="bad_kind")
    if scope not in {"batch", "class"}:
        raise ChangeRequestRefused(f"unknown scope {scope!r}", code="bad_scope")
    if not isinstance(day, str) or not _DAY_PATTERN.match(day.strip()):
        raise ChangeRequestRefused("invalid day", code="bad_day")
    if not isinstance(start_time, str) or not start_time.strip():
        raise ChangeRequestRefused("missing start_time", code="bad_slot")

    requester_batch_safe = _safe_batch(requester_batch)
    if scope == "class":
        # Derive (and validate) the class prefix upfront so a malformed batch
        # is rejected at create time, not surfaced later during approval.
        _safe_class_prefix(requester_batch_safe)

    if kind in {"add", "edit"} and not isinstance(entry, dict):
        raise ChangeRequestRefused(
            f"{kind} requires an 'entry' object",
            code="missing_entry",
        )
    if kind == "delete" and entry is not None:
        # Be forgiving but ignore caller-provided entry on delete; storage
        # treats delete as a slot wipe regardless.
        entry = None

    # Class-scope is only allowed for Lecture changes (matches frontend
    # business rule: lab/tutorial sectioning may differ per batch).
    if scope == "class" and kind in {"add", "edit"}:
        entry_type = (entry or {}).get("type")
        if entry_type != "Lecture":
            raise ChangeRequestRefused(
                "class scope is only allowed for Lecture entries",
                code="scope_requires_lecture",
            )

    # Resolve current semester so the request is anchored to the live one
    # regardless of when the admin reviews it.
    current = await read_current()
    semester_label = current["label"]

    # Server-side flood guards (extra to HTTP rate limiter).
    if requester_id:
        per_user = await ChangeRequestDoc.find(
            ChangeRequestDoc.requester_id == requester_id,
            ChangeRequestDoc.status == "pending",
        ).count()
        if per_user >= MAX_PENDING_PER_REQUESTER:
            raise ChangeRequestRefused(
                "too many pending requests from this user",
                code="quota_user",
            )
    per_batch = await ChangeRequestDoc.find(
        ChangeRequestDoc.requester_batch == requester_batch_safe,
        ChangeRequestDoc.status == "pending",
    ).count()
    if per_batch >= MAX_PENDING_PER_BATCH:
        raise ChangeRequestRefused(
            "too many pending requests for this batch",
            code="quota_batch",
        )
    total = await ChangeRequestDoc.find(
        ChangeRequestDoc.status == "pending"
    ).count()
    if total >= MAX_PENDING_TOTAL:
        raise ChangeRequestRefused(
            "override queue is full; try again later",
            code="quota_global",
        )

    # Dupe guard: identical pending request for same (batch, scope, slot, kind)
    dupe = await ChangeRequestDoc.find_one(
        ChangeRequestDoc.requester_batch == requester_batch_safe,
        ChangeRequestDoc.scope == scope,
        ChangeRequestDoc.kind == kind,
        ChangeRequestDoc.day == day,
        ChangeRequestDoc.start_time == start_time,
        ChangeRequestDoc.status == "pending",
    )
    if dupe is not None:
        raise ChangeRequestRefused(
            "an identical pending request already exists",
            code="duplicate",
        )

    doc = ChangeRequestDoc(
        requester_id=requester_id,
        requester_batch=requester_batch_safe,
        semester=semester_label,
        scope=scope,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        day=day.strip(),
        start_time=start_time.strip(),
        entry=entry,  # type: ignore[arg-type]
    )
    await doc.insert()
    return _serialize_change_request(doc)


async def list_change_requests(
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if status is not None and status not in {"pending", "approved", "rejected"}:
        raise ValueError(f"invalid status {status!r}")
    query = ChangeRequestDoc.find_all(sort=[("created_at", -1)])
    if status:
        query = ChangeRequestDoc.find(ChangeRequestDoc.status == status, sort=[("created_at", -1)])
    out: list[dict[str, Any]] = []
    async for doc in query:
        out.append(_serialize_change_request(doc))
        if len(out) >= limit:
            break
    return out


async def _resolve_target_batches(scope: str, requester_batch: str) -> list[str]:
    """Expand scope into the actual list of batch codes to mutate."""
    if scope == "batch":
        return [_safe_batch(requester_batch)]
    prefix = _safe_class_prefix(requester_batch)
    codes: list[str] = []
    async for doc in BatchDoc.find_all(sort=[("code", 1)]):
        if doc.code.startswith(prefix):
            codes.append(doc.code)
    if not codes:
        # Fall back to timetables so an unseeded BatchDoc collection still works.
        seen: set[str] = set()
        async for tt in TimetableDoc.find_all(sort=[("code", 1)]):
            if tt.code.startswith(prefix) and tt.code not in seen:
                seen.add(tt.code)
                codes.append(tt.code)
    return codes


def _apply_change_to_classes(
    classes: list[Any],
    *,
    kind: str,
    day: str,
    start_time: str,
    entry: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Pure function: apply add/edit/delete to a list of class entries."""
    out: list[dict[str, Any]] = []
    target_key = (day, start_time)
    replaced = False
    for c in classes:
        c_dict = _serialize_class(c)
        if (c_dict.get("day"), c_dict.get("start_time")) == target_key:
            if kind == "delete":
                continue
            if kind == "edit":
                merged = {**c_dict, **(entry or {})}
                merged["day"] = day
                merged["start_time"] = start_time
                out.append(merged)
                replaced = True
                continue
        out.append(c_dict)
    if kind == "add" and entry is not None:
        new_entry = {**entry, "day": day, "start_time": start_time}
        out.append(new_entry)
    elif kind == "edit" and not replaced and entry is not None:
        # Slot was empty in the canonical data — treat the edit as an add.
        new_entry = {**entry, "day": day, "start_time": start_time}
        out.append(new_entry)
    return out


async def approve_change_request(
    request_id: str,
    *,
    decided_by: str | None = None,
    decision_note: str | None = None,
) -> dict[str, Any]:
    from bson import ObjectId  # local import: only needed by admin path

    try:
        oid = ObjectId(request_id)
    except Exception as exc:  # noqa: BLE001 — any bson decode error
        raise ChangeRequestRefused("invalid request id", code="bad_id") from exc

    doc = await ChangeRequestDoc.get(oid)
    if doc is None:
        raise ChangeRequestRefused("override not found", code="not_found")
    if doc.status != "pending":
        raise ChangeRequestRefused(
            f"request already {doc.status}",
            code="not_pending",
        )

    targets = await _resolve_target_batches(doc.scope, doc.requester_batch)
    if not targets:
        raise ChangeRequestRefused(
            "no matching batches found in scope",
            code="empty_scope",
        )

    entry_payload = (
        doc.entry.model_dump(exclude_none=False) if doc.entry is not None else None
    )
    applied: list[str] = []
    for code in targets:
        tt = await TimetableDoc.find_one(TimetableDoc.code == code)
        if tt is None:
            continue
        new_classes = _apply_change_to_classes(
            tt.classes,
            kind=doc.kind,
            day=doc.day,
            start_time=doc.start_time,
            entry=entry_payload,
        )
        await tt.set({
            "classes": new_classes,
            "updated_at": datetime.now(timezone.utc),
        })
        applied.append(code)
        settings = get_settings()
        if settings.json_mirror:
            path = settings.data_dir / "timetable" / f"{code}.json"
            _mirror_json(
                path,
                _timetable_payload_from_raw(code, tt.semester, new_classes),
            )

    if not applied:
        raise ChangeRequestRefused(
            "no canonical timetables found for resolved batches",
            code="empty_targets",
        )

    await doc.set({
        "status": "approved",
        "decided_by": decided_by,
        "decision_note": decision_note,
        "decided_at": datetime.now(timezone.utc),
        "applied_batches": applied,
    })
    return _serialize_change_request(doc)


async def reject_change_request(
    request_id: str,
    *,
    decided_by: str | None = None,
    decision_note: str | None = None,
) -> dict[str, Any]:
    from bson import ObjectId

    try:
        oid = ObjectId(request_id)
    except Exception as exc:  # noqa: BLE001
        raise ChangeRequestRefused("invalid request id", code="bad_id") from exc
    doc = await ChangeRequestDoc.get(oid)
    if doc is None:
        raise ChangeRequestRefused("override not found", code="not_found")
    if doc.status != "pending":
        raise ChangeRequestRefused(
            f"request already {doc.status}",
            code="not_pending",
        )
    await doc.set({
        "status": "rejected",
        "decided_by": decided_by,
        "decision_note": decision_note,
        "decided_at": datetime.now(timezone.utc),
    })
    return _serialize_change_request(doc)


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


# ── Admin emails (allowlist) ─────────────────────────────────────────────
_ADMIN_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _safe_admin_email(email: str) -> str:
    cleaned = (email or "").strip().lower()
    if not _ADMIN_EMAIL_PATTERN.match(cleaned):
        raise ValueError("email must look like name@domain.tld")
    return cleaned


async def is_admin_email(email: str) -> bool:
    if not email:
        return False
    cleaned = email.strip().lower()
    settings = get_settings()
    if cleaned in settings.admin_emails:
        return True
    doc = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
    return doc is not None


async def count_admin_emails() -> int:
    return await AdminEmailDoc.find_all().count()


async def list_admin_emails() -> list[dict[str, Any]]:
    """Combine env-set + Mongo-set admins. Env entries marked ``source="env"``."""
    settings = get_settings()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for email in sorted(settings.admin_emails):
        out.append({
            "email": email,
            "display_name": None,
            "source": "env",
            "added_at": None,
            "added_by": "env",
        })
        seen.add(email)
    async for doc in AdminEmailDoc.find_all(sort=[("added_at", -1)]):
        if doc.email in seen:
            continue
        out.append({
            "email": doc.email,
            "display_name": doc.display_name,
            "source": "db",
            "added_at": doc.added_at.isoformat() if doc.added_at else None,
            "added_by": doc.added_by,
        })
    return out


async def add_admin_email(
    email: str,
    *,
    display_name: str | None = None,
    added_by: str | None = None,
) -> dict[str, Any]:
    cleaned = _safe_admin_email(email)
    settings = get_settings()
    if cleaned in settings.admin_emails:
        return {
            "email": cleaned,
            "display_name": display_name,
            "source": "env",
            "added_at": None,
            "added_by": "env",
        }
    existing = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
    if existing is not None:
        if display_name is not None and (display_name or None) != existing.display_name:
            await existing.set({"display_name": display_name or None})
            existing = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
        return {
            "email": existing.email,
            "display_name": existing.display_name,
            "source": "db",
            "added_at": existing.added_at.isoformat() if existing.added_at else None,
            "added_by": existing.added_by,
        }
    doc = AdminEmailDoc(
        email=cleaned,
        display_name=display_name or None,
        added_by=added_by,
    )
    await doc.insert()
    return {
        "email": doc.email,
        "display_name": doc.display_name,
        "source": "db",
        "added_at": doc.added_at.isoformat(),
        "added_by": doc.added_by,
    }


async def delete_admin_email(email: str) -> bool:
    cleaned = _safe_admin_email(email)
    settings = get_settings()
    if cleaned in settings.admin_emails:
        raise ValueError("env-managed admin; remove from ADMIN_EMAILS env var")
    doc = await AdminEmailDoc.find_one(AdminEmailDoc.email == cleaned)
    if doc is None:
        return False
    await doc.delete()
    return True


# ── Upload attempts (audit log + dashboard fuel) ─────────────────────────
def _serialize_upload_attempt(doc: UploadAttemptDoc, *, include_errors: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(doc.id),
        "started_at": doc.started_at.isoformat() if doc.started_at else None,
        "finished_at": doc.finished_at.isoformat() if doc.finished_at else None,
        "actor_kind": doc.actor_kind,
        "actor_email": doc.actor_email,
        "filename": doc.filename,
        "sheet_selector": doc.sheet_selector,
        "semester_label": doc.semester_label,
        "status": doc.status,
        "batches_written": doc.batches_written,
        "classes_written": doc.classes_written,
        "sheets_used": list(doc.sheets_used or []),
        "multi_sheet_batches": list(doc.multi_sheet_batches or []),
        "total_blocks": doc.total_blocks,
        "confidence_summary": dict(doc.confidence_summary or {}),
        "error_count": doc.error_count,
        "doctor": doc.doctor,
        "failure_message": doc.failure_message,
    }
    if include_errors:
        payload["errors"] = [
            row.model_dump(exclude_none=False) if hasattr(row, "model_dump") else dict(row)
            for row in (doc.errors or [])
        ]
    return payload


async def record_upload_attempt(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a single UploadAttemptDoc. Best-effort — never raises."""
    try:
        errors_raw = payload.get("errors") or []
        errors_models = []
        for row in errors_raw:
            if isinstance(row, UploadErrorRow):
                errors_models.append(row)
            elif isinstance(row, dict):
                errors_models.append(UploadErrorRow(**row))
        doc = UploadAttemptDoc(
            started_at=payload.get("started_at") or datetime.now(timezone.utc),
            finished_at=payload.get("finished_at") or datetime.now(timezone.utc),
            actor_kind=payload.get("actor_kind"),
            actor_email=payload.get("actor_email"),
            filename=payload.get("filename"),
            sheet_selector=payload.get("sheet_selector"),
            semester_label=payload.get("semester_label"),
            status=payload.get("status") or "ok",
            batches_written=int(payload.get("batches_written") or 0),
            classes_written=int(payload.get("classes_written") or 0),
            sheets_used=list(payload.get("sheets_used") or []),
            multi_sheet_batches=list(payload.get("multi_sheet_batches") or []),
            total_blocks=int(payload.get("total_blocks") or 0),
            confidence_summary=dict(payload.get("confidence_summary") or {}),
            error_count=int(payload.get("error_count") or len(errors_models)),
            errors=errors_models,
            doctor=payload.get("doctor"),
            failure_message=payload.get("failure_message"),
        )
        await doc.insert()
        return _serialize_upload_attempt(doc, include_errors=False)
    except Exception:
        logger.exception("failed to persist UploadAttemptDoc")
        return {}


async def list_upload_attempts(
    *,
    limit: int = 50,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in {"ok", "partial", "failed"}:
        raise ValueError(f"invalid status {status!r}")
    if status:
        query = UploadAttemptDoc.find(UploadAttemptDoc.status == status).sort(-UploadAttemptDoc.started_at)
    else:
        query = UploadAttemptDoc.find_all().sort(-UploadAttemptDoc.started_at)
    out: list[dict[str, Any]] = []
    async for doc in query:
        out.append(_serialize_upload_attempt(doc, include_errors=False))
        if len(out) >= limit:
            break
    return out


async def get_upload_attempt(attempt_id: str) -> dict[str, Any] | None:
    from bson import ObjectId

    try:
        oid = ObjectId(attempt_id)
    except Exception:
        return None
    doc = await UploadAttemptDoc.get(oid)
    if doc is None:
        return None
    return _serialize_upload_attempt(doc, include_errors=True)


async def get_latest_upload_attempt() -> dict[str, Any] | None:
    doc = await UploadAttemptDoc.find_all().sort(-UploadAttemptDoc.started_at).first_or_none()
    if doc is None:
        return None
    return _serialize_upload_attempt(doc, include_errors=True)


async def compute_admin_stats() -> dict[str, Any]:
    """Aggregate numbers for the admin dashboard hero cards + donut.

    All-time aggregates across every recorded ingest.
    """
    batches_with_timetables = await TimetableDoc.find_all().count()

    total_uploads = await UploadAttemptDoc.find_all().count()
    failed_partial = await UploadAttemptDoc.find(
        {"status": {"$in": ["partial", "failed"]}}
    ).count()

    total_errors = 0
    total_blocks = 0
    high = medium = low = unreliable = 0
    async for doc in UploadAttemptDoc.find_all():
        total_errors += int(doc.error_count or 0)
        total_blocks += int(doc.total_blocks or 0)
        summary = dict(doc.confidence_summary or {})
        high += int(summary.get("HIGH", 0))
        medium += int(summary.get("MEDIUM", 0))
        low += int(summary.get("LOW", 0))
        unreliable += int(summary.get("UNRELIABLE", 0))

    parsed_ok = high + medium
    accuracy_pct: float | None = None
    if total_blocks > 0:
        accuracy_pct = round(parsed_ok * 100.0 / total_blocks, 2)

    return {
        "batches_with_timetables": batches_with_timetables,
        "uploads_logged": total_uploads,
        "failed_partial_uploads": failed_partial,
        "total_parsing_errors": total_errors,
        "parsing_accuracy_pct": accuracy_pct,
        "confidence_totals": {
            "HIGH": high,
            "MEDIUM": medium,
            "LOW": low,
            "UNRELIABLE": unreliable,
            "TOTAL": total_blocks,
        },
    }


# ── Announcements + exam dates ───────────────────────────────────────────
# Both collections seed once from the curated JSON under ``assets/`` the
# first time they're read empty, so the public sidebar feeds keep working
# during the JSON → Mongo cutover without manual reimport.

_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_ALLOWED_SEVERITY = {"info", "warn", "critical"}


def _parse_iso_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("posted_at must be an ISO-8601 datetime") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _announcement_payload(doc: AnnouncementDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "title": doc.title,
        "body": doc.body,
        "severity": doc.severity,
        "posted_at": _iso_z(doc.posted_at),
        "link": doc.link,
    }


def _exam_payload(doc: ExamDateDoc) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "subject": doc.subject,
        "code": doc.code,
        "date": doc.date,
        "slot": doc.slot,
        "type": doc.type,
        "room": doc.room,
        "target_year": doc.target_year,
    }


async def _seed_announcements_if_empty() -> None:
    if await AnnouncementDoc.find_all().count() > 0:
        return
    path = _ASSETS_DIR / "announcements.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("announcement seed skipped: %s", exc)
        return
    if not isinstance(data, list):
        return
    docs: list[AnnouncementDoc] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        body = str(raw.get("body") or "").strip()
        if not title or not body:
            continue
        severity = str(raw.get("severity") or "info").strip().lower()
        if severity not in _ALLOWED_SEVERITY:
            severity = "info"
        try:
            posted_at = _parse_iso_dt(raw.get("posted_at") or datetime.now(timezone.utc))
        except ValueError:
            posted_at = datetime.now(timezone.utc)
        link = raw.get("link")
        if link is not None and not isinstance(link, str):
            link = None
        docs.append(
            AnnouncementDoc(
                title=title,
                body=body,
                severity=severity,
                posted_at=posted_at,
                link=link or None,
            )
        )
    if docs:
        await AnnouncementDoc.insert_many(docs)


async def _seed_exam_dates_if_empty() -> None:
    if await ExamDateDoc.find_all().count() > 0:
        return
    path = _ASSETS_DIR / "exam_dates.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("exam-dates seed skipped: %s", exc)
        return
    if not isinstance(data, list):
        return
    docs: list[ExamDateDoc] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject") or "").strip()
        code = str(raw.get("code") or "").strip().upper()
        date = str(raw.get("date") or "").strip()
        if not subject or not code or not date:
            continue
        docs.append(
            ExamDateDoc(
                subject=subject,
                code=code,
                date=date,
                slot=(raw.get("slot") or None),
                type=(raw.get("type") or None),
                room=(raw.get("room") or None),
            )
        )
    if docs:
        await ExamDateDoc.insert_many(docs)


async def list_announcements() -> list[dict[str, Any]]:
    await _seed_announcements_if_empty()
    return [
        _announcement_payload(doc)
        async for doc in AnnouncementDoc.find_all(sort=[("posted_at", -1)])
    ]


async def add_announcement(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    title = str(payload.get("title") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not title:
        raise ValueError("title is required")
    if not body:
        raise ValueError("body is required")
    severity = str(payload.get("severity") or "info").strip().lower()
    if severity not in _ALLOWED_SEVERITY:
        raise ValueError("severity must be one of: info, warn, critical")
    posted_at_raw = payload.get("posted_at")
    posted_at = _parse_iso_dt(posted_at_raw) if posted_at_raw else datetime.now(timezone.utc)
    link = payload.get("link")
    if link is not None and not isinstance(link, str):
        raise ValueError("link must be a string when provided")
    doc = AnnouncementDoc(
        title=title,
        body=body,
        severity=severity,
        posted_at=posted_at,
        link=(link or None) if link is not None else None,
    )
    await doc.insert()
    return _announcement_payload(doc)


async def delete_announcement(announcement_id: str) -> bool:
    from beanie import PydanticObjectId
    try:
        oid = PydanticObjectId(announcement_id)
    except Exception:  # invalid id format → treated as not-found
        return False
    doc = await AnnouncementDoc.get(oid)
    if doc is None:
        return False
    await doc.delete()
    return True


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def list_exam_dates(batch: str | None = None) -> list[dict[str, Any]]:
    """Return all exam-date rows, earliest-first.

    When a ``batch`` code is supplied, the result is filtered to:
      * exams whose ``target_year`` matches the batch's year (or is ``None``);
      * exams whose ``code`` appears in that batch's timetable (canonical
        cells *and* elective option codes).

    If the batch has no timetable yet, only the year filter is applied so
    the user still sees something useful. When no batch is supplied (e.g.
    the home page) every exam row is returned.
    """
    await _seed_exam_dates_if_empty()

    subject_codes: set[str] | None = None
    user_year: int | None = None
    if batch:
        try:
            code = _safe_batch(batch)
        except BatchNotFound:
            code = None
        if code:
            meta = _derive_batch_meta(code)
            user_year = meta.get("year")
            subject_codes = await _collect_batch_subject_codes(code)

    out: list[dict[str, Any]] = []
    async for doc in ExamDateDoc.find_all(sort=[("date", 1), ("slot", 1)]):
        if user_year is not None and doc.target_year is not None and doc.target_year != user_year:
            continue
        if subject_codes is not None and doc.code.upper() not in subject_codes:
            continue
        out.append(_exam_payload(doc))
    return out


async def _collect_batch_subject_codes(batch: str) -> set[str]:
    """Return the upper-cased subject codes present in ``batch``'s timetable.

    Includes both canonical cell codes and any elective ``options[*].subject_code``.
    Empty set on miss (or no timetable).
    """
    doc = await TimetableDoc.find_one(TimetableDoc.code == batch)
    if doc is None:
        return set()
    codes: set[str] = set()
    for klass in doc.classes:
        code = (klass.code or "").strip().upper()
        if code:
            codes.add(code)
        for opt in klass.options or []:
            opt_code = (opt.subject_code or "").strip().upper()
            if opt_code:
                codes.add(opt_code)
    return codes


async def add_exam_date(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    subject = str(payload.get("subject") or "").strip()
    code = str(payload.get("code") or "").strip().upper()
    date = str(payload.get("date") or "").strip()
    if not subject:
        raise ValueError("subject is required")
    if not code:
        raise ValueError("code is required")
    if not _DATE_RE.match(date):
        raise ValueError("date must be yyyy-mm-dd")

    def _opt(field: str) -> str | None:
        raw = payload.get(field)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(f"{field} must be a string when provided")
        cleaned = raw.strip()
        return cleaned or None

    target_year: int | None = None
    raw_year = payload.get("target_year")
    if raw_year not in (None, "", "all"):
        try:
            target_year = int(raw_year)
        except (TypeError, ValueError) as exc:
            raise ValueError("target_year must be an integer (1..9) or null") from exc
        if not 1 <= target_year <= 9:
            raise ValueError("target_year must be between 1 and 9")

    doc = ExamDateDoc(
        subject=subject,
        code=code,
        date=date,
        slot=_opt("slot"),
        type=_opt("type"),
        room=_opt("room"),
        target_year=target_year,
    )
    await doc.insert()
    return _exam_payload(doc)


async def delete_exam_date(exam_id: str) -> bool:
    from beanie import PydanticObjectId
    try:
        oid = PydanticObjectId(exam_id)
    except Exception:
        return False
    doc = await ExamDateDoc.get(oid)
    if doc is None:
        return False
    await doc.delete()
    return True
