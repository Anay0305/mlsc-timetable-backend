"""Cross-batch sanity check.

Every batch within a ``{YEAR}{ALPHA}`` group (e.g. all ``3C**``) is compared
against an **admin-curated baseline** for that group. Groups without a baseline
are surfaced as ``no_baseline`` advisories; nothing is inferred from the data
itself. The report contains:

* the expected per-type breakdown (from the baseline), and
* any batches whose actual counts deviate from that expected breakdown.

The same report is produced from two contexts:

* in-memory after an ingest (via :func:`build_doctor_report`)
* on-disk over the JSON mirror (via :func:`doctor_report_from_dir`, used by the
  ``doctor`` CLI subcommand)
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


TOTAL_KEY = "total"


def count_classes(classes: Iterable[Any]) -> dict[str, Any]:
    """Count classes by type, total, and subject-code/type."""
    counts: dict[str, int] = {}
    course_counts: dict[str, dict[str, int]] = {}
    n = 0
    for c in classes:
        t = _entry_type(c)
        counts[t] = counts.get(t, 0) + 1
        code = _base_code(_entry_code(c))
        if code:
            per_course = course_counts.setdefault(code, {})
            per_course[t] = per_course.get(t, 0) + 1
        n += 1
    counts[TOTAL_KEY] = n
    counts["_course_counts"] = course_counts
    return counts


def codes_in(classes: Iterable[Any]) -> set[str]:
    """Return the set of subject codes present in a batch's class entries."""
    out: set[str] = set()
    for c in classes:
        code = _entry_code(c)
        if code:
            out.add(_base_code(code))
        for opt in _entry_options(c):
            opt_code = _entry_code(opt)
            if opt_code:
                out.add(_base_code(opt_code))
    return out


def build_doctor_report(
    counts_by_batch: Mapping[str, Mapping[str, int]],
    *,
    baselines_by_group: Mapping[str, Mapping[str, int]] | None = None,
    semester_prefix: str | None = None,
    codes_by_batch: Mapping[str, Iterable[str]] | None = None,
    courses_by_group: Mapping[str, Iterable[Any]] | None = None,
) -> dict[str, Any]:
    """Group `counts_by_batch` by ``code[:2]`` and compare each batch to its
    admin baseline.

    `counts_by_batch` maps each batch code to a ``{type: count}`` dict
    (plus a derived ``"total"`` entry, see :func:`count_classes`).

    `baselines_by_group` maps the bare ``{YEAR}{ALPHA}`` group (e.g. ``"1A"``)
    to its expected per-type counts. Groups without a baseline are reported
    under ``no_baseline`` and not compared.

    When ``codes_by_batch`` and ``courses_by_group`` are supplied each group
    entry (both ``ok`` and ``mismatches``) additionally carries a
    ``course_check`` field listing per-batch missing/extra course codes. A
    group with only course-code drift is promoted from ``ok`` to
    ``mismatches``.
    """
    baselines_by_group = dict(baselines_by_group or {})
    codes_by_batch_norm: dict[str, set[str]] = {
        b: {c.strip().upper() for c in codes if c}
        for b, codes in (codes_by_batch or {}).items()
    }
    courses_by_group_norm: dict[str, list[Any]] = {
        g: list(courses)
        for g, courses in (courses_by_group or {}).items()
    }

    groups: dict[str, list[str]] = defaultdict(list)
    for code in counts_by_batch:
        if len(code) < 2 or not code[0].isdigit() or not code[1].isalpha():
            groups["??"].append(code)
            continue
        groups[code[:2].upper()].append(code)

    ok: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    no_baseline: list[dict[str, Any]] = []

    for group in sorted(groups):
        codes = sorted(groups[group])
        batch_counts = {c: dict(counts_by_batch[c]) for c in codes}
        baseline_key = (
            f"{semester_prefix.upper()}{group}"
            if semester_prefix and group != "??"
            else None
        )

        baseline = baselines_by_group.get(group)
        if baseline is None:
            no_baseline.append({
                "group": group,
                "baseline_key": baseline_key,
                "batches": len(codes),
                "batch_codes": codes,
            })
            continue

        # A baseline may exist with no per-type counts yet (e.g. only a course
        # roster was uploaded via the scheme-PDF flow). In that case skip the
        # count comparison entirely — no false MISMATCH, no false MISSING.
        has_counts = bool(baseline)
        expected = _expand_baseline(baseline) if has_counts else {}

        outliers: list[dict[str, Any]] = []
        if has_counts:
            for code in codes:
                deltas = _diff_counts(batch_counts[code], expected)
                if deltas:
                    outliers.append({
                        "batch": code,
                        "counts": batch_counts[code],
                        "deltas": deltas,
                    })

        entry: dict[str, Any] = {
            "group": group,
            "baseline_key": baseline_key,
            "expected": expected,
            "expected_source": "baseline" if has_counts else "none",
            "batches": len(codes),
            "matching": len(codes) - len(outliers),
        }

        course_check = _build_course_check(
            group,
            codes,
            codes_by_batch_norm,
            counts_by_batch,
            courses_by_group_norm,
        )
        if course_check is not None:
            entry["course_check"] = course_check

        has_count_drift = bool(outliers)
        has_course_drift = bool(course_check and course_check.get("has_drift"))

        if has_count_drift:
            entry["outliers"] = outliers
        if has_count_drift or has_course_drift:
            mismatches.append(entry)
        else:
            ok.append(entry)

    return {
        "total_batches": len(counts_by_batch),
        "total_groups": len(groups),
        "consistent_groups": len(ok),
        "mismatched_groups": len(mismatches),
        "groups_without_baseline": len(no_baseline),
        "ok": ok,
        "mismatches": mismatches,
        "no_baseline": no_baseline,
    }


def doctor_report_from_dir(
    timetable_dir: Path,
    *,
    baselines_by_group: Mapping[str, Mapping[str, int]] | None = None,
    semester_prefix: str | None = None,
) -> dict[str, Any]:
    """Build a doctor report by reading every ``<batch>.json`` in `timetable_dir`."""
    counts: dict[str, dict[str, int]] = {}
    for path in sorted(timetable_dir.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        code = payload.get("batch") or path.stem
        counts[code] = count_classes(payload.get("classes") or [])
    return build_doctor_report(
        counts,
        baselines_by_group=baselines_by_group,
        semester_prefix=semester_prefix,
    )


def format_doctor_report(report: Mapping[str, Any]) -> str:
    """Render a doctor report as human-readable text (used by the CLI)."""
    lines: list[str] = []
    lines.append(
        f"doctor report ({report['total_batches']} batches across "
        f"{report['total_groups']} groups)"
    )
    lines.append("=" * 64)

    ok = list(report.get("ok") or [])
    if ok:
        lines.append(f"OK ({len(ok)} groups, all batches match):")
        for row in ok:
            lines.append(f"  {_group_header(row)}")

    no_baseline = list(report.get("no_baseline") or [])
    if no_baseline:
        lines.append("")
        lines.append(f"NO BASELINE ({len(no_baseline)} groups, skipped):")
        for row in no_baseline:
            key = row.get("baseline_key") or row.get("group")
            lines.append(f"  {row['group']}: {row['batches']} batches (define baseline {key})")

    mismatches = list(report.get("mismatches") or [])
    if mismatches:
        lines.append("")
        lines.append(f"MISMATCH ({len(mismatches)} groups):")
        for row in mismatches:
            lines.append(
                f"  {_group_header(row)}  ({row['matching']}/{row['batches']} batches match)"
            )
            for out in row.get("outliers") or []:
                pieces = ", ".join(
                    f"{k} {_sign(v)}" for k, v in sorted(out["deltas"].items())
                )
                lines.append(
                    f"      {out['batch']}: total={out['counts'].get(TOTAL_KEY, 0)}  [{pieces}]"
                )
    elif not no_baseline:
        lines.append("")
        lines.append("all groups consistent")
    return "\n".join(lines)


# ── helpers ──────────────────────────────────────────────────────────────
def _entry_type(entry: Any) -> str:
    if isinstance(entry, Mapping):
        value = entry.get("type")
    else:
        value = getattr(entry, "type", None)
    if not isinstance(value, str) or not value.strip():
        return "Unknown"
    return value.strip()


def _entry_code(entry: Any) -> str | None:
    if isinstance(entry, Mapping):
        value = entry.get("code") or entry.get("subject_code")
    else:
        value = getattr(entry, "code", None) or getattr(entry, "subject_code", None)
    return value if isinstance(value, str) and value.strip() else None


def _entry_options(entry: Any) -> Iterable[Any]:
    if isinstance(entry, Mapping):
        opts = entry.get("options") or []
    else:
        opts = getattr(entry, "options", None) or []
    return opts if isinstance(opts, Iterable) else []


def _build_course_check(
    group: str,
    batches: list[str],
    codes_by_batch: Mapping[str, set[str]],
    counts_by_batch: Mapping[str, Mapping[str, int]],
    courses_by_group: Mapping[str, list[Any]],
) -> dict[str, Any] | None:
    """Compare course presence and expected L/T/P occurrences per batch."""
    raw_courses = courses_by_group.get(group)
    if not raw_courses:
        return None

    expected_courses = _normalise_expected_courses(raw_courses)
    if not expected_courses:
        return None
    expected_set = set(expected_courses)
    per_batch: dict[str, dict[str, list[str]]] = {}
    detailed: dict[str, dict[str, Any]] = {}
    matching = 0
    for batch in batches:
        observed = codes_by_batch.get(batch, set())
        missing = sorted(expected_set - observed)
        extra = sorted(observed - expected_set)
        per_batch[batch] = {"missing": missing, "extra": extra}
        actual_course_counts = _course_counts_for_batch(batch, counts_by_batch)
        course_deltas: list[dict[str, Any]] = []
        for code, course_info in expected_courses.items():
            expected = course_info["counts"]
            actual = actual_course_counts.get(code, {})
            if not expected and not actual:
                continue  # legacy code-only roster with no count data
            types = sorted(set(expected) | set(actual))
            deltas = {
                type_name: int(actual.get(type_name, 0)) - int(expected.get(type_name, 0))
                for type_name in types
                if int(actual.get(type_name, 0)) != int(expected.get(type_name, 0))
            }
            if deltas:
                course_deltas.append({
                    "code": code,
                    "title": course_info.get("title"),
                    "expected": dict(expected),
                    "actual": dict(actual),
                    "deltas": deltas,
                })
        missing_details = [
            {
                "code": code,
                "title": expected_courses[code].get("title"),
                "expected": dict(expected_courses[code]["counts"]),
                "actual": dict(actual_course_counts.get(code, {})),
            }
            for code in missing
        ]
        extra_details = [
            {
                "code": code,
                "title": None,
                "expected": {},
                "actual": dict(actual_course_counts.get(code, {})),
            }
            for code in extra
        ]
        detailed[batch] = {
            "missing": missing,
            "extra": extra,
            "missing_details": missing_details,
            "extra_details": extra_details,
            "actual_course_counts": actual_course_counts,
            "course_deltas": course_deltas,
        }
        if not missing and not extra and not course_deltas:
            matching += 1

    batches_with_missing = [
        {"batch": batch, "missing": value["missing"], "course_details": value["missing_details"]}
        for batch, value in detailed.items() if value["missing"]
    ]
    batches_with_extra = [
        {"batch": batch, "extra": value["extra"], "course_details": value["extra_details"]}
        for batch, value in detailed.items() if value["extra"]
    ]
    batches_with_count_drift = [
        {"batch": batch, "course_deltas": value["course_deltas"]}
        for batch, value in detailed.items() if value["course_deltas"]
    ]
    return {
        "expected_codes": sorted(expected_set),
        "expected_course_details": [
            {"code": code, "title": info.get("title"), "expected": dict(info["counts"])}
            for code, info in sorted(expected_courses.items())
        ],
        "expected_count": len(expected_set),
        "per_batch": per_batch,
        "per_batch_detail": detailed,
        "batches_with_missing": batches_with_missing,
        "batches_with_extra": batches_with_extra,
        "batches_with_count_drift": batches_with_count_drift,
        "matching": matching,
        "batches": len(batches),
        "has_drift": bool(batches_with_missing or batches_with_extra or batches_with_count_drift),
    }


def _normalise_expected_courses(courses: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Convert baseline course rows into ``base_code -> expected type counts``."""
    result: dict[str, dict[str, int]] = {}
    for course in courses:
        if isinstance(course, str):
            code = course
            values = {}
        elif isinstance(course, Mapping):
            code = course.get("code")
            values = course
        else:
            code = getattr(course, "code", None)
            values = {
                "L": getattr(course, "L", None),
                "T": getattr(course, "T", None),
                "P": getattr(course, "P", None),
            }
        base = _base_code(code)
        if not base:
            continue
        expected_counts = {
            type_name: _numeric_value(_course_field(values, letter, type_name))
            for letter, type_name in (("L", "Lecture"), ("T", "Tutorial"), ("P", "Practical"))
        }
        expected_counts = {key: value for key, value in expected_counts.items() if value > 0}
        result[base] = {
            "title": values.get("title") if isinstance(values, Mapping) else None,
            "counts": expected_counts,
        }
    return result


def _course_counts_for_batch(
    batch: str,
    counts_by_batch: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    """Read per-course counts attached to a batch by ``count_classes``."""
    raw = counts_by_batch.get(batch, {})
    value = raw.get("_course_counts", {}) if isinstance(raw, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _numeric_value(value: Any) -> int:
    if value is None:
        return 0
    numbers = re.findall(r"\d+(?:\.\d+)?", str(value))
    try:
        return int(sum(float(number) for number in numbers))
    except ValueError:
        return 0


def _course_field(values: Mapping[str, Any], letter: str, type_name: str) -> Any:
    """Read scheme columns despite Mongo/JSON/client casing differences."""
    aliases = {
        letter,
        letter.lower(),
        type_name,
        type_name.lower(),
        f"{type_name.lower()}s",
    }
    for key, value in values.items():
        if str(key).strip().lower() in {alias.lower() for alias in aliases}:
            return value
    return None
def _base_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    code = value.strip().upper()
    return code[:-1] if code and code[-1] in "LTP" else code


def _expand_baseline(baseline: Mapping[str, int]) -> dict[str, int]:
    expected = {
        k: int(v) for k, v in baseline.items()
        if k != TOTAL_KEY and not k.startswith("_")
    }
    expected[TOTAL_KEY] = sum(expected.values())
    return expected


def _diff_counts(actual: Mapping[str, int], expected: Mapping[str, int]) -> dict[str, int]:
    types = (set(actual) | set(expected)) - {"_course_counts"}
    out: dict[str, int] = {}
    for t in types:
        a = int(actual.get(t, 0))
        e = int(expected.get(t, 0))
        if a != e:
            out[t] = a - e
    return out


def _group_header(row: Mapping[str, Any]) -> str:
    expected = row.get("expected") or {}
    parts = [f"{v} {k}" for k, v in sorted(expected.items()) if k != TOTAL_KEY and not k.startswith("_")]
    breakdown = " / ".join(parts) if parts else "(empty)"
    total = expected.get(TOTAL_KEY, sum(v for k, v in expected.items() if k != TOTAL_KEY and not k.startswith("_")))
    baseline_key = row.get("baseline_key") or "?"
    return f"{row['group']}: {breakdown} = {total} (baseline {baseline_key})  · {row['batches']} batches"


def _sign(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)
