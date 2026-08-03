"""Validate user keyword export and clear in-process alias cache."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from extraction_aliases_store import USER_KEYWORDS_PATH, clear_user_alias_cache


def main() -> int:
    if USER_KEYWORDS_PATH.exists():
        try:
            json.loads(USER_KEYWORDS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
            return 1
    clear_user_alias_cache()
    print(
        json.dumps(
            {
                "ok": True,
                "path": str(USER_KEYWORDS_PATH),
                "exists": USER_KEYWORDS_PATH.exists(),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
