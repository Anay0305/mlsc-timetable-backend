"""Subject catalog lookup for the internal parser."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


DEFAULT_SUBJECTS_PATH = Path(__file__).resolve().parents[3] / "assets" / "subjects.json"


@dataclass(frozen=True)
class SubjectCatalog:
    subjects: dict[str, str]

    @classmethod
    def load(cls, path: Path = DEFAULT_SUBJECTS_PATH) -> "SubjectCatalog":
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(subjects={str(code).upper(): str(name) for code, name in data.items()})

    def name_for(self, subject_code: str | None) -> str | None:
        if subject_code is None:
            return None
        return self.subjects.get(base_subject_code(subject_code))


@lru_cache(maxsize=1)
def load_default_subject_catalog() -> SubjectCatalog:
    return SubjectCatalog.load()


def base_subject_code(subject_code: str) -> str:
    return subject_code.strip().upper()[:-1]
