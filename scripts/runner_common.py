"""Shared helpers for consolidated extraction runner scripts."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEMO_DATA_DIR = BACKEND_DIR / "Demo_Data"

# Prefer years next to report wording; never pull years out of timestamp IDs
# like ``369_1614941592096.pdf`` (that wrongly became 2096).
_REPORT_YEAR_RE = re.compile(
    r"(?i)(?:annual|quarterly|interim|report|year|fy)[^\d]{0,24}((?:19|20)\d{2})"
)
_STANDALONE_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def resolve_report_year(*sources: Any) -> int | None:
    """Infer the reporting year from folder/group names, then filename.

    Folder names like ``Annual report 2020`` win over PDF ids that happen to
    contain digit runs such as ``…1592096.pdf``.
    """
    texts = [str(s).strip() for s in sources if s]
    if not texts:
        return None

    for text in texts:
        m = _REPORT_YEAR_RE.search(text)
        if m:
            year = int(m.group(1))
            if 1990 <= year <= 2100:
                return year

    for text in texts:
        years = [
            int(y)
            for y in _STANDALONE_YEAR_RE.findall(text)
            if 1990 <= int(y) <= 2100
        ]
        if years:
            # Prefer the year printed in a short folder label over a long path.
            return max(years) if len(text) > 40 else years[-1]

    return None


def configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_log(text: str, level: str = "info") -> None:
    for raw in str(text).splitlines():
        line = raw.rstrip()
        if line.strip():
            emit({"type": "log", "level": level, "message": line})


def parse_items_payload(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = raw.get("items") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        company = str(entry.get("company") or "").strip()
        report_type = str(entry.get("report_type") or "").strip()
        file_name = str(entry.get("file_name") or "").strip()
        rel_path = str(entry.get("rel_path") or "").strip()
        group = str(entry.get("group") or "").strip()
        if not (company and report_type and file_name):
            continue
        out.append(
            {
                "company": company,
                "report_type": report_type,
                "file_name": file_name,
                "rel_path": rel_path,
                "group": group,
            }
        )
    return out


def is_quarterly_type(report_type: str) -> bool:
    t = (report_type or "").strip().lower()
    return any(
        tok in t
        for tok in (
            "quarter",
            "interim",
            "q1",
            "q2",
            "q3",
            "q4",
            "half year",
            "halfyear",
            "half-year",
        )
    )


def filter_items_by_kind(
    items: list[dict[str, str]], kind: str
) -> list[dict[str, str]]:
    if kind == "all":
        return items
    if kind == "quarterly":
        return [it for it in items if is_quarterly_type(it.get("report_type", ""))]
    return [it for it in items if not is_quarterly_type(it.get("report_type", ""))]
