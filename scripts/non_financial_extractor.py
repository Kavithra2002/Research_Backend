"""
non_financial_extractor.py
==========================
Consolidated non-financial data extraction runner.

Wraps non_financial_data_run.py for structured non-financial metrics.
"""
from __future__ import annotations

import argparse
import io
import json
import sys

from runner_common import configure_stdio, emit, parse_items_payload

configure_stdio()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items-stdin", action="store_true", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--model", default="gpt-4o")
    ap.add_argument("--apikey", default=None)
    args = ap.parse_args(argv)

    try:
        raw = json.loads(sys.stdin.read())
        items = parse_items_payload(raw)
    except Exception as exc:
        emit({"type": "error", "message": f"Failed to parse items: {exc!r}"})
        return 2

    if not items:
        emit({"type": "error", "message": "No reports selected."})
        return 2

    import non_financial_data_run

    forward = ["--items-stdin", "--model", args.model]
    if args.dry_run:
        forward.append("--dry-run")
    if args.apikey:
        forward.extend(["--apikey", args.apikey])

    sys.stdin = io.StringIO(json.dumps({"items": items}))
    return non_financial_data_run.main(forward)


if __name__ == "__main__":
    raise SystemExit(main())
