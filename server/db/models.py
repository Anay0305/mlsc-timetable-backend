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


ALL_DOCUMENTS = [
    SemesterDoc,
    BatchDoc,
    TimetableDoc,
    UserDoc,
    OverrideDoc,
    BaselineDoc,
    ContributorDoc,
    ChangeRequestDoc,
]
