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
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


TOTAL_KEY = "total"


def count_classes(classes: Iterable[Any]) -> dict[str, int]:
    """Count classes by ``type`` plus a derived ``total``."""
    counts: dict[str, int] = {}
    n = 0
    for c in classes:
        t = _entry_type(c)
        counts[t] = counts.get(t, 0) + 1
        n += 1
    counts[TOTAL_KEY] = n
    return counts


def codes_in(classes: Iterable[Any]) -> set[str]:
    """Return the set of subject codes present in a batch's class entries."""
    out: set[str] = set()
    for c in classes:
        code = _entry_code(c)
        if code:
            out.add(code.strip().upper())
        for opt in _entry_options(c):
            opt_code = _entry_code(opt)
            if opt_code:
                out.add(opt_code.strip().upper())
    return out


def build_doctor_report(
    counts_by_batch: Mapping[str, Mapping[str, int]],
    *,
    baselines_by_group: Mapping[str, Mapping[str, int]] | None = None,
    semester_prefix: str | None = None,
    codes_by_batch: Mapping[str, Iterable[str]] | None = None,
    courses_by_group: Mapping[str, Iterable[str]] | None = None,
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
    courses_by_group_norm: dict[str, list[str]] = {
        g: [c.strip().upper() for c in codes if c]
        for g, codes in (courses_by_group or {}).items()
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
    courses_by_group: Mapping[str, list[str]],
) -> dict[str, Any] | None:
    """Compare each batch's observed subject codes against the group's expected
    course roster. Returns None when no roster exists for the group.
    """
    expected = courses_by_group.get(group)
    if not expected:
        return None
    expected_set = set(expected)
    per_batch: dict[str, dict[str, list[str]]] = {}
    matching = 0
    for batch in batches:
        observed = codes_by_batch.get(batch, set())
        missing = sorted(expected_set - observed)
        # "extra" = present in timetable but not in the expected roster. We
        # intentionally do NOT flag electives here because their codes vary
        # per student; the admin can still spot them in the extra list.
        extra = sorted(observed - expected_set)
        per_batch[batch] = {"missing": missing, "extra": extra}
        if not missing:
            matching += 1
    return {
        "expected_codes": sorted(expected_set),
        "expected_count": len(expected_set),
        "per_batch": per_batch,
        "matching": matching,
        "batches": len(batches),
        "has_drift": any(v["missing"] for v in per_batch.values()),
    }


def _expand_baseline(baseline: Mapping[str, int]) -> dict[str, int]:
    expected = {k: int(v) for k, v in baseline.items() if k != TOTAL_KEY}
    expected[TOTAL_KEY] = sum(expected.values())
    return expected


def _diff_counts(actual: Mapping[str, int], expected: Mapping[str, int]) -> dict[str, int]:
    types = set(actual) | set(expected)
    out: dict[str, int] = {}
    for t in types:
        a = int(actual.get(t, 0))
        e = int(expected.get(t, 0))
        if a != e:
            out[t] = a - e
    return out


def _group_header(row: Mapping[str, Any]) -> str:
    expected = row.get("expected") or {}
    parts = [f"{v} {k}" for k, v in sorted(expected.items()) if k != TOTAL_KEY]
    breakdown = " / ".join(parts) if parts else "(empty)"
    total = expected.get(TOTAL_KEY, sum(v for k, v in expected.items() if k != TOTAL_KEY))
    baseline_key = row.get("baseline_key") or "?"
    return f"{row['group']}: {breakdown} = {total} (baseline {baseline_key})  · {row['batches']} batches"


def _sign(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)
