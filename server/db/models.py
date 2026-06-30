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


class BaselineDoc(Document):
    """Expected per-type class count for a `{semester_prefix}{YEAR}{ALPHA}` group.

    `key` is e.g. ``"E1A"`` (Even-semester 1st-year stream A) or ``"O3C"``
    (Odd-semester 3rd-year stream C). `counts` maps a class type
    (``"Lecture"``, ``"Tutorial"``, ``"Practical"``, etc.) to the expected
    occurrences per batch in that group. The doctor compares observed
    counts against this and flags any deviating batch.
    """

    key: Annotated[str, Indexed(unique=True)]
    semester_prefix: str  # "E" or "O"
    group: str            # "1A", "3C", ...
    counts: dict[str, int] = Field(default_factory=dict)
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


class UploadErrorRow(BaseModel):
    """One parser warning surfaced on the admin dashboard."""

    batch: Optional[str] = None
    sheet: Optional[str] = None
    day: Optional[str] = None
    start_time: Optional[str] = None
    severity: str = "MEDIUM"  # confidence level (HIGH|MEDIUM|LOW|UNRELIABLE)
    code: str
    message: str


class UploadAttemptDoc(Document):
    """One historical record of a `/admin/ingest` invocation.

    Persisted whether the run succeeded, partially succeeded, or threw. Drives
    the admin dashboard cards, the parsing-error log, and the accuracy donut.
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
    error_count: int = 0
    errors: list[UploadErrorRow] = Field(default_factory=list)
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
]
