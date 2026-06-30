from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    admin_secret: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(admin_secret=os.getenv("TIMETABLE_ADMIN_SECRET"))
