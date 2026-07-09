"""Beanie document models.

Shapes mirror the existing HTTP contract so the API output stays byte-identical
with the previous JSON-on-disk backend. New collections (`users`, `overrides`)
support the per-user override flow without changing canonical reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from beanie import Document, Indexed
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, IndexModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Embedded value objects ────────────────────────────────────────────────
class ElectiveOption(BaseModel):
    model_config = ConfigDict(extra="allow")
    subject_code: Optional[str] = None
    subject_name: Optional[str] = None
    type: str = "Unknown"
    place: Optional[str] = None
    teacher: Optional[str] = None


class ClassEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    day: str
    start_time: str
    end_time: str
    subject: Optional[str] = None
    code: Optional[str] = None
    type: str = "Unknown"
    room: Optional[str] = None
    options: list[ElectiveOption] = Field(default_factory=list)


class TimetableSource(BaseModel):
    file: Optional[str] = None
    sheet: Optional[str] = None
    ingested_at: Optional[datetime] = None


# ── Documents ─────────────────────────────────────────────────────────────
class SemesterDoc(Document):
    """Singleton — only one doc with `key == "current"` ever exists."""

    key: Annotated[str, Indexed(unique=True)] = "current"
    label: str
    term_end_dates: Optional[dict] = None  # {"1": "2026-11-15", "2": "2026-11-30", ...} per UG year
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "semester"


class BatchDoc(Document):
    code: Annotated[str, Indexed(unique=True)]
    year: Optional[int] = None
    section: Optional[str] = None
    source_sheet: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "batches"


class TimetableDoc(Document):
    code: Annotated[str, Indexed(unique=True)]
    semester: str
    classes: list[ClassEntry]
    source: Optional[TimetableSource] = None
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "timetables"


class UserDoc(Document):
    user_id: Annotated[str, Indexed(unique=True)]
    display_name: Optional[str] = None
    default_batch: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "users"


class OverrideEntry(BaseModel):
    """A single per-cell override.

    For `delete` no `entry` is needed; for everything else `entry` carries the
    full class shape the client should render in place of the canonical cell.
    """

    kind: Literal["elective_pick", "edit", "delete", "add"]
    entry: Optional[ClassEntry] = None


class OverrideDoc(Document):
    """All overrides for one (user, batch) bundled into a single document.

    Keys in `entries` are `f"{day}|{start_time}"` (e.g. "Monday|9:00 AM").
    A user's batch is fixed for the semester so we don't index on semester
    separately — the batch already implies it.
    """

    user_id: str
    batch: str
    entries: dict[str, OverrideEntry] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "overrides"
        indexes = [
            [("user_id", 1), ("batch", 1)],
        ]


class BaselineCourseCheck(BaseModel):
    """One expected course entry under a baseline.

    Sourced from the SUGC/SPGC course-scheme PDF via the ``POST /admin/scheme``
    flow. ``code`` may be ``None`` for placeholder rows (e.g. ``ELECTIVE-II``)
    where the specific code is chosen per student. L/T/P/Cr are kept as strings
    so alternate-week markers like ``"1*"`` and combined counts like ``"14+1*"``
    survive the round-trip from the PDF.
    """

    model_config = ConfigDict(extra="allow")

    code: Optional[str] = None
    title: Optional[str] = None
    category: Optional[str] = None
    L: Optional[str] = None
    T: Optional[str] = None
    P: Optional[str] = None
    Cr: Optional[str] = None


class BaselineDoc(Document):
    """Expected per-type class count for a `{semester_prefix}{YEAR}{ALPHA}` group.

    `key` is e.g. ``"E1A"`` (Even-semester 1st-year stream A) or ``"O3C"``
    (Odd-semester 3rd-year stream C). `counts` maps a class type
    (``"Lecture"``, ``"Tutorial"``, ``"Practical"``, etc.) to the expected
    occurrences per batch in that group. The doctor compares observed
    counts against this and flags any deviating batch.

    ``courses`` optionally holds the roster of courses expected for that
    baseline, sourced from a SUGC/SPGC course-scheme PDF. When present the
    doctor also verifies that each expected course code appears at least once
    in every batch's ingested timetable.
    """

    key: Annotated[str, Indexed(unique=True)]
    semester_prefix: str  # "E" or "O"
    group: str            # "1A", "3C", ...
    counts: dict[str, int] = Field(default_factory=dict)
    courses: list[BaselineCourseCheck] = Field(default_factory=list)
    scheme_source: Optional[str] = None  # filename of the PDF that supplied the courses
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "baselines"


class ContributorDoc(Document):
    """A community contributor whose avatar is sourced live from GitHub.

    We deliberately store only the GitHub username — avatar URLs and display
    names are fetched on demand from the GitHub REST API so they always
    reflect the user's current profile picture without manual upkeep.
    """

    username: Annotated[str, Indexed(unique=True)]
    display_name: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "contributors"


class ChangeRequestDoc(Document):
    """Crowd-sourced proposal to mutate a canonical timetable.

    A user editing their grid can promote the change to either their whole
    batch or — for ``Lecture`` types — the whole class (all batches sharing
    the first 3 characters of the batch code, e.g. ``1B11/1B12/1B13``).
    Admins review pending requests and approve/reject them; approval
    rewrites the canonical timetable for every batch in scope.
    """

    requester_id: Optional[str] = None
    requester_batch: str
    semester: str
    scope: Literal["batch", "class"]
    kind: Literal["add", "edit", "delete"]
    day: str
    start_time: str
    entry: Optional[ClassEntry] = None
    status: Literal["pending", "approved", "rejected"] = "pending"
    decision_note: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    applied_batches: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "change_requests"
        indexes = [
            "status",
            [("status", 1), ("created_at", -1)],
        ]


class AdminEmailDoc(Document):
    """An email address allowed to use the admin panel.

    Env-managed admins (``ADMIN_EMAILS`` env var) are *not* stored here;
    only emails granted via the panel (or a direct mongo insert) live in
    this collection. The auth dep checks env-set ∪ this collection. Emails
    are stored lowercased.
    """

    email: Annotated[str, Indexed(unique=True)]
    display_name: Optional[str] = None
    added_by: Optional[str] = None  # email of the admin who added this entry, or "token"
    added_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "admin_emails"


class UploadAttemptDoc(Document):
    """One historical record of a `/admin/ingest` invocation.

    Persisted whether the run succeeded, partially succeeded, or threw. Drives
    the admin dashboard cards, the parsing-error log, and the accuracy donut.
    Per-error rows live in the ``ParsingErrorDoc`` collection keyed by
    ``upload_id`` — they are the single source of truth for triage counts.
    """

    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: Optional[datetime] = None
    actor_kind: Optional[str] = None  # "user" | "token" | "cli"
    actor_email: Optional[str] = None
    filename: Optional[str] = None
    sheet_selector: Optional[str] = None
    semester_label: Optional[str] = None
    status: Literal["ok", "partial", "failed"] = "ok"
    batches_written: int = 0
    classes_written: int = 0
    sheets_used: list[str] = Field(default_factory=list)
    multi_sheet_batches: list[dict] = Field(default_factory=list)
    total_blocks: int = 0
    confidence_summary: dict[str, int] = Field(default_factory=dict)
    doctor: Optional[dict] = None
    failure_message: Optional[str] = None

    class Settings:
        name = "upload_attempts"
        indexes = [
            [("started_at", -1)],
            "status",
        ]


class AnnouncementDoc(Document):
    """Site-wide announcement shown on the public landing sidebar.

    Curated by admins via the panel. ``severity`` drives a colour dot in the
    UI (``info`` / ``warn`` / ``critical``). ``posted_at`` is what the public
    list sorts by (latest first).
    """

    title: str
    body: str
    severity: Literal["info", "warn", "critical"] = "info"
    posted_at: datetime = Field(default_factory=_utcnow)
    link: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "announcements"
        indexes = [
            [("posted_at", -1)],
        ]


class ExamDateDoc(Document):
    """Upcoming exam slot shown on the public landing sidebar.

    ``date`` is intentionally a yyyy-mm-dd *string* (not a Mongo date) to
    keep the API contract byte-identical with the previous JSON store and
    make sort/equality trivial. ``target_year`` scopes the exam to a single
    year (1..5) — when ``None`` the exam is broadcast to every year.
    """

    subject: str
    code: str
    date: str  # yyyy-mm-dd
    slot: Optional[str] = None
    type: Optional[str] = None
    room: Optional[str] = None
    target_year: Optional[int] = None  # None = all years
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "exam_dates"
        indexes = [
            [("date", 1), ("slot", 1)],
            "target_year",
        ]


class CalendarOverrideDoc(Document):
    """Semester-calendar override for a single date on the mini-calendar.

    Two kinds:
      * ``holiday``     — the date is a declared holiday (no class);
                          ``reason`` is optional (e.g. "Diwali", "Rain day").
      * ``follow_day``  — the date runs another weekday's timetable
                          (``follows_day`` is 0..4 = Mon..Fri).
                          Typical use: "this Saturday follows Monday's schedule".

    Scope determines who sees the override:
      * ``global``      — everyone (default)
      * ``year``        — only batches whose year (1..5) is in ``scope_values``
                          (values are stringified ints, e.g. ["1", "2"]).
      * ``branch``      — only batches whose "year+stream" prefix
                          (e.g. "2A", "1E") is in ``scope_values``.
    """

    date: str  # yyyy-mm-dd
    kind: Literal["holiday", "follow_day", "mst", "est", "assessment", "frosh"]
    reason: Optional[str] = None
    follows_day: Optional[int] = None  # 0..4 (Mon..Fri); required when kind=follow_day
    scope: Literal["global", "year", "branch"] = "global"
    scope_values: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "calendar_overrides"
        indexes = [
            [("date", 1)],
            "scope",
        ]


class CalendarConnectionDoc(Document):
    """Stores a user's Google Calendar OAuth tokens + sync state.

    Tokens are Fernet-encrypted. ``calendar_id`` is the id of the dedicated
    'MLSC Timetable' calendar we created in the user's Google account.
    ``batch_code`` is the batch whose timetable is currently synced.
    """

    user_id: Annotated[str, Indexed(unique=True)]  # Clerk sub claim
    refresh_token: str       # Fernet-encrypted
    access_token: str        # Fernet-encrypted, short-lived cache
    access_expires_at: datetime
    google_email: str
    calendar_id: Optional[str] = None
    batch_code: Optional[str] = None
    enabled: bool = False
    created_at: datetime = Field(default_factory=_utcnow)
    last_synced_at: Optional[datetime] = None
    last_error: Optional[str] = None

    class Settings:
        name = "calendar_connections"


class CalendarSyncJobDoc(Document):
    """One queued sync task for a user.

    The background worker polls ``status='pending'`` rows. Retries use
    exponential backoff up to ``_MAX_ATTEMPTS`` (5). ``updated_at`` doubles
    as the 'not before' timestamp for retries.
    """

    user_id: str
    trigger: Literal["initial", "override_changed", "batch_changed", "manual", "retry"]
    override_id: Optional[str] = None
    status: Literal["pending", "running", "done", "failed"] = "pending"
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "calendar_sync_jobs"
        indexes = [
            [("status", 1), ("updated_at", 1)],
            [("user_id", 1), ("status", 1)],
        ]


class CalendarEventMapDoc(Document):
    """Maps a stable slot_id to the Google Calendar event we created for it.

    Used as a fast lookup cache; the true source of identity is the
    ``extendedProperties.private.mlscSlotId`` on the Google event itself.
    """

    user_id: str
    slot_id: str
    google_event_id: str
    rrule_hash: str

    class Settings:
        name = "calendar_event_maps"
        indexes = [
            [("user_id", 1), ("slot_id", 1)],
        ]


class IngestSnapshotDoc(Document):
    """Pre-ingest backup of the live data so admins can roll back the most
    recent ``/admin/ingest`` run.

    We only ever keep **one** snapshot at a time — the next ingest replaces it.
    A TTL index on ``expires_at`` makes Mongo auto-delete the snapshot after
    ``INGEST_SNAPSHOT_TTL_HOURS`` (default 24h), so a stale snapshot can't be
    used to undo a run from days ago.
    """

    created_at: datetime = Field(default_factory=_utcnow)
    expires_at: datetime
    semester_label: Optional[str] = None
    batch_count: int = 0
    timetable_count: int = 0
    # Full row dumps (as plain dicts) keyed under each list. We do NOT use
    # Document references because the goal is to be self-contained — restoring
    # works even if the underlying collections were emptied.
    batches: list[dict] = Field(default_factory=list)
    timetables: list[dict] = Field(default_factory=list)
    current: Optional[dict] = None  # semester doc snapshot

    class Settings:
        name = "ingest_snapshots"
        # TTL index: Mongo deletes the doc when expires_at < now.
        indexes = [
            IndexModel([("expires_at", ASCENDING)], expireAfterSeconds=0),
            [("created_at", -1)],
        ]


class ParsingErrorDoc(Document):
    """One persisted parser warning / doctor mismatch.

    Populated by ``/admin/ingest``; surfaced on the admin Fix tab where each
    row can be opened, navigated to in the timetable editor, and marked as
    ``resolved`` or ``ignored`` (with an optional admin note).
    """

    upload_id: Optional[str] = None  # str(UploadAttemptDoc.id)
    batch_code: Optional[str] = None
    error_type: Annotated[str, Indexed()]
    severity: Literal["info", "warn", "error"] = "warn"
    day: Optional[str] = None
    start_time: Optional[str] = None
    period: Optional[int] = None
    code: Optional[str] = None  # subject code involved (if any)
    message: str
    context: dict = Field(default_factory=dict)  # raw extra (sheet, source row, etc.)

    status: Annotated[Literal["open", "resolved", "ignored"], Indexed()] = "open"
    resolved_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    note: Optional[str] = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "parsing_errors"
        indexes = [
            [("upload_id", 1), ("status", 1)],
            [("batch_code", 1), ("status", 1)],
            [("created_at", -1)],
        ]


class SubjectDoc(Document):
    """Subject-code → human-readable name mapping.

    Replaces the on-disk ``assets/subjects.json`` at runtime. The JSON file
    is still used as a *seed* on first boot (when the collection is empty);
    after that, every read goes through this collection so admins can add
    missing codes live from the Fix tab to clear ``SUBJECT_NOT_IN_CATALOG``
    parser errors without a re-ingest.

    ``code`` is the normalized form: upper-cased with the trailing L/T/P
    suffix stripped (matches ``base_subject_code()``), so a single row
    covers ``UPH013L``, ``UPH013T`` and ``UPH013P``.
    """

    code: Annotated[str, Indexed(unique=True)]
    name: str
    aliases: list[str] = Field(default_factory=list)
    source: Literal["seed", "admin", "import"] = "seed"
    created_by: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    class Settings:
        name = "subjects"


ALL_DOCUMENTS = [
    SemesterDoc,
    BatchDoc,
    TimetableDoc,
    UserDoc,
    OverrideDoc,
    BaselineDoc,
    ContributorDoc,
    ChangeRequestDoc,
    AdminEmailDoc,
    UploadAttemptDoc,
    AnnouncementDoc,
    ExamDateDoc,
    CalendarOverrideDoc,
    CalendarConnectionDoc,
    CalendarSyncJobDoc,
    CalendarEventMapDoc,
    IngestSnapshotDoc,
    ParsingErrorDoc,
    SubjectDoc,
]
