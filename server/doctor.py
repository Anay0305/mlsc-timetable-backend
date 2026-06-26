"""Cross-batch sanity check.

Every batch within a ``{YEAR}{ALPHA}`` group (e.g. all ``3C**``) should have the
same per-type class breakdown. The doctor reports:

* the expected breakdown (from an admin-curated **baseline** when present,
  otherwise from the per-type mode across the group's batches), and
* any batches whose actual counts deviate from the expected.

The same report is produced from two contexts:

* in-memory after an ingest (via :func:`build_doctor_report`)
* on-disk over the JSON mirror (via :func:`doctor_report_from_dir`, used by the
  ``doctor`` CLI subcommand)
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
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


def build_doctor_report(
    counts_by_batch: Mapping[str, Mapping[str, int]],
    *,
    baselines_by_group: Mapping[str, Mapping[str, int]] | None = None,
    semester_prefix: str | None = None,
) -> dict[str, Any]:
    """Group `counts_by_batch` by ``code[:2]`` and report per-type consistency.

    `counts_by_batch` maps each batch code to a ``{type: count}`` dict
    (plus a derived ``"total"`` entry, see :func:`count_classes`).

    `baselines_by_group` maps the bare ``{YEAR}{ALPHA}`` group (e.g. ``"1A"``)
    to its expected per-type counts. When supplied, the doctor compares against
    these instead of the per-type mode.
    """
    baselines_by_group = dict(baselines_by_group or {})

    groups: dict[str, list[str]] = defaultdict(list)
    for code in counts_by_batch:
        if len(code) < 2 or not code[0].isdigit() or not code[1].isalpha():
            groups["??"].append(code)
            continue
        groups[code[:2].upper()].append(code)

    ok: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []

    for group in sorted(groups):
        codes = sorted(groups[group])
        batch_counts = {c: dict(counts_by_batch[c]) for c in codes}

        baseline = baselines_by_group.get(group)
        if baseline:
            expected = _expand_baseline(baseline)
            expected_source = "baseline"
        else:
            expected = _mode_counts(batch_counts.values())
            expected_source = "mode"

        outliers: list[dict[str, Any]] = []
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
            "baseline_key": (
                f"{semester_prefix.upper()}{group}"
                if semester_prefix and group != "??"
                else None
            ),
            "expected": expected,
            "expected_source": expected_source,
            "batches": len(codes),
            "matching": len(codes) - len(outliers),
        }
        if outliers:
            entry["outliers"] = outliers
            mismatches.append(entry)
        else:
            ok.append(entry)

    return {
        "total_batches": len(counts_by_batch),
        "total_groups": len(groups),
        "consistent_groups": len(ok),
        "mismatched_groups": len(mismatches),
        "ok": ok,
        "mismatches": mismatches,
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
    else:
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


def _expand_baseline(baseline: Mapping[str, int]) -> dict[str, int]:
    expected = {k: int(v) for k, v in baseline.items() if k != TOTAL_KEY}
    expected[TOTAL_KEY] = sum(expected.values())
    return expected


def _mode_counts(batches: Iterable[Mapping[str, int]]) -> dict[str, int]:
    """Per-type mode across every batch in the group."""
    batch_list = list(batches)
    types: set[str] = set()
    for counts in batch_list:
        types.update(counts.keys())
    result: dict[str, int] = {}
    for t in sorted(types):
        tally = Counter(int(c.get(t, 0)) for c in batch_list)
        result[t] = tally.most_common(1)[0][0]
    return result


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
    source = row.get("expected_source") or "?"
    baseline_key = row.get("baseline_key")
    tag = (
        f"baseline {baseline_key}" if (source == "baseline" and baseline_key) else source
    )
    return f"{row['group']}: {breakdown} = {total} ({tag})  · {row['batches']} batches"


def _sign(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)
