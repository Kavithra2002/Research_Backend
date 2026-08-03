"""
Load user-managed extraction keywords from MongoDB export JSON and merge
with built-in LABEL_ALIASES / DRIVERS_LABEL_ALIASES / QUARTERLY_LABEL_ALIASES.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
USER_KEYWORDS_PATH = BACKEND_DIR / "New_Updates" / "extraction_keywords_user.json"

Scope = str  # "fs" | "drivers" | "quarterly"


def _load_user_entries() -> list[dict[str, Any]]:
    if not USER_KEYWORDS_PATH.exists():
        return []
    try:
        payload = json.loads(USER_KEYWORDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = payload.get("entries")
    return entries if isinstance(entries, list) else []


@lru_cache(maxsize=1)
def user_aliases_by_scope() -> dict[Scope, dict[str, list[str]]]:
    out: dict[Scope, dict[str, list[str]]] = {
        "fs": {},
        "drivers": {},
        "quarterly": {},
    }
    for entry in _load_user_entries():
        if not isinstance(entry, dict):
            continue
        scope = str(entry.get("scope") or "fs").strip().lower()
        if scope not in out:
            continue
        label = str(entry.get("canonical_label") or "").strip()
        if not label:
            continue
        aliases = entry.get("aliases") or []
        clean = [str(a).strip() for a in aliases if str(a).strip()]
        out[scope][label] = clean
    return out


def clear_user_alias_cache() -> None:
    user_aliases_by_scope.cache_clear()


def merge_alias_map(
    base: dict[str, list[str]],
    scope: Scope,
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {k: list(v) for k, v in base.items()}
    user = user_aliases_by_scope().get(scope, {})
    for label, extras in user.items():
        if label in merged:
            seen = {a.lower() for a in merged[label]}
            for alias in extras:
                if alias.lower() not in seen:
                    merged[label].append(alias)
                    seen.add(alias.lower())
        else:
            merged[label] = list(extras) if extras else [label]
    return merged


def patterns_for_label(
    template_label: str,
    base_map: dict[str, list[str]],
    scope: Scope,
    *,
    default_to_label: bool = True,
) -> list[str]:
    merged = merge_alias_map(base_map, scope)
    patterns = list(merged.get(template_label, []))
    # Always keep the workbook canonical label in the search set — user
    # synonyms alone can miss abbreviated PDF wording (and vice versa).
    if default_to_label and template_label not in patterns:
        patterns.append(template_label)
    if not patterns and default_to_label:
        patterns = [template_label]
    # Prefer longer / more specific phrases first.
    patterns.sort(key=lambda p: len((p or "").strip()), reverse=True)
    return patterns
