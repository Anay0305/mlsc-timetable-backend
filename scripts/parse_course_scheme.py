"""CLI wrapper around :func:`server.scheme_parser.parse_scheme_pdf`.

Usage:
    python scripts/parse_course_scheme.py <path-to-pdf> [--out out.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Support running as a plain script (no package install required).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server.scheme_parser import parse_scheme_pdf  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Parse baseline semester tables from a Thapar course scheme PDF.",
    )
    ap.add_argument("pdf", type=Path, help="Path to the course scheme PDF")
    ap.add_argument("--out", type=Path, default=None,
                    help="Write JSON to this path (default: stdout)")
    args = ap.parse_args()

    if not args.pdf.exists():
        print(f"error: {args.pdf} not found", file=sys.stderr)
        return 1

    result = parse_scheme_pdf(args.pdf)
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"wrote {args.out} ({len(result['semesters'])} semesters)", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
