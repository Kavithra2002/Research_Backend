"""Print built-in canonical extraction labels + aliases as JSON (one line)."""
from __future__ import annotations

import json
import sys

from extraction_catalog import build_catalog


def main() -> int:
    payload = build_catalog(refresh_manifest=True)
    print(json.dumps(payload), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
