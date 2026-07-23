"""Subject-code → name catalog.

Runtime source of truth is the ``subjects`` collection in MongoDB; the
on-disk ``assets/subjects.json`` is used only as a seed when the collection
is empty (first boot, or after an intentional reset).

The parser is sync, so we keep a process-local immutable ``SubjectCatalog``
snapshot. The async helpers below load / invalidate that snapshot — admin
writes (POST/PATCH/DELETE on ``/admin/subjects``) bump the version so the
next ``ensure_catalog()`` call rebuilds from Mongo.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_SUBJECTS_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "subjects.json"
)


@dataclass(frozen=True)
class SubjectCatalog:
    subjects: dict[str, str]

    @classmethod
    def from_pairs(cls, pairs) -> "SubjectCatalog":
        return cls(
            subjects={str(code).upper(): normalize_subject_name(str(name)) for code, name in pairs}
        )

    @classmethod
    def load_from_file(cls, path: Path = DEFAULT_SUBJECTS_PATH) -> "SubjectCatalog":
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls.from_pairs(data.items())

    @classmethod
    def empty(cls) -> "SubjectCatalog":
        return cls(subjects={})

    def name_for(self, subject_code: Optional[str]) -> Optional[str]:
        if subject_code is None:
            return None
        code = subject_code.strip().upper()
        return self.subjects.get(code) or self.subjects.get(base_subject_code(code))


def base_subject_code(subject_code: str) -> str:
    return subject_code.strip().upper()[:-1]


# ── Process-local catalog cache ──────────────────────────────────────────
# Set by ``ensure_catalog()``; bumped by ``invalidate_catalog()``. Sync code
# (the parser) reads via ``get_cached_catalog()``; if it's still ``None``
# we fall back to the file so we never crash a parse on a cold cache.

_lock = threading.Lock()
_catalog: Optional[SubjectCatalog] = None
_version: int = 0


def get_cached_catalog() -> SubjectCatalog:
    """Sync accessor used by the parser. Falls back to the on-disk file if
    Mongo hasn't been queried yet — keeps the parser usable in scripts/tests
    that don't go through ``server.app.lifespan``.
    """
    with _lock:
        if _catalog is not None:
            return _catalog
    try:
        return SubjectCatalog.load_from_file()
    except FileNotFoundError:
        return SubjectCatalog.empty()


def _set_cached(catalog: SubjectCatalog) -> None:
    global _catalog, _version
    with _lock:
        _catalog = catalog
        _version += 1


def invalidate_catalog() -> None:
    """Drop the in-process snapshot; next ``ensure_catalog()`` rebuilds."""
    global _catalog
    with _lock:
        _catalog = None


def catalog_version() -> int:
    with _lock:
        return _version


async def ensure_catalog() -> SubjectCatalog:
    """Return a fresh catalog snapshot from Mongo, rebuilding the cache if
    it was invalidated. Safe to call from any async handler — cheap when
    the cache is warm (returns the existing snapshot).
    """
    with _lock:
        if _catalog is not None:
            return _catalog
    # Late import: this module is also pulled in by sync parser code that
    # must not require the Beanie ODM to be initialized.
    from server.db.models import SubjectDoc  # noqa: WPS433

    pairs: list[tuple[str, str]] = []
    async for doc in SubjectDoc.find_all():
        pairs.append((doc.code, doc.name))
    catalog = SubjectCatalog.from_pairs(pairs)
    _set_cached(catalog)
    return catalog


async def seed_subjects_from_file_if_empty(
    path: Path = DEFAULT_SUBJECTS_PATH,
) -> int:
    """First-boot helper: if the ``subjects`` collection is empty and the
    seed file exists, bulk-insert it with ``source="seed"``. Returns the
    number of rows written.
    """
    from server.db.models import SubjectDoc  # noqa: WPS433

    existing = await SubjectDoc.find_all().count()
    if existing > 0:
        return 0
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    docs = [
        SubjectDoc(code=str(code).upper(), name=str(name), source="seed")
        for code, name in data.items()
        if code and name
    ]
    if not docs:
        return 0
    await SubjectDoc.insert_many(docs)
    invalidate_catalog()
    return len(docs)


# ── Backwards-compatible shims ───────────────────────────────────────────
# Old call sites used ``load_default_subject_catalog()`` (sync). Keep the
# name so we don't have to touch every parser file, but route it through
# the new cache.

def load_default_subject_catalog() -> SubjectCatalog:
    return get_cached_catalog()
def normalize_subject_name(value: str) -> str:
    acronyms = {"AI", "API", "CPU", "GPU", "IoT", "ML", "NLP", "UCS", "UI", "URL", "XML"}
    words = []
    for word in " ".join(str(value or "").split()).split(" "):
        bare = word.strip("()[],.:;/-")
        words.append(word if bare.upper() in {item.upper() for item in acronyms} else word[:1].upper() + word[1:].lower())
    return " ".join(words)
