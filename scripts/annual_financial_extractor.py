"""
annual_financial_extractor.py
=============================
Main annual financial statement extraction runner.

Wraps Demo_run.py for annual reports only (OpenAI vision pipeline + Mongo upload).
"""
from __future__ import annotations

import argparse
import json
import sys

from runner_common import configure_stdio, emit, filter_items_by_kind, parse_items_payload

configure_stdio()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items-stdin", action="store_true", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--model", default="gpt-5")
    ap.add_argument("--option", choices=["1", "2"], default="1")
    ap.add_argument("--apikey", default=None)
    ap.add_argument("--mongo-uri", default=None)
    ap.add_argument("--db-name", default=None)
    args = ap.parse_args(argv)

    import io

    try:
        raw = json.loads(sys.stdin.read())
        items = filter_items_by_kind(parse_items_payload(raw), "annual")
    except Exception as exc:
        emit({"type": "error", "message": f"Failed to parse items: {exc!r}"})
        return 2

    if not items:
        emit({"type": "error", "message": "No annual reports selected."})
        return 2

    import Demo_run

    forward = [
        "--items-stdin",
        "--model",
        args.model,
        "--option",
        args.option,
    ]
    if args.dry_run:
        forward.append("--dry-run")
    if args.force:
        forward.append("--force")
    if args.no_upload:
        forward.append("--no-upload")
    if args.apikey:
        forward.extend(["--apikey", args.apikey])
    if args.mongo_uri:
        forward.extend(["--mongo-uri", args.mongo_uri])
    if args.db_name:
        forward.extend(["--db-name", args.db_name])

    sys.stdin = io.StringIO(json.dumps({"items": items}))
    return Demo_run.main(forward)


if __name__ == "__main__":
    raise SystemExit(main())
