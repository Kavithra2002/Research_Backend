"""Fixed on-disk home for comparison-page capture images.

Layout (stable across restarts)::

    Demo_Data_captures/<Company>/<Annual|Quarterly>/<YYYY>/<statement_key>/page_XXXX.png
    Demo_Data_captures/<Company>/<Annual|Quarterly>/<YYYY>/index.json

``index.json`` is the fixed list the comparison page reads. Re-running a
capture for a year replaces that year's main-statement images and rewrites
the index. Note folders (``note_*``) are left in place.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
DEMO_CAPTURES_OUT = BACKEND_DIR / "Demo_Data_captures"

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
INDEX_NAME = "index.json"


def year_capture_dir(
    company: str,
    period: str,
    year: int,
    *,
    root: Path | None = None,
) -> Path:
    period_name = "Quarterly" if str(period).strip().lower().startswith("q") else "Annual"
    return (root or DEMO_CAPTURES_OUT) / company / period_name / str(int(year))


def _is_note_dir(name: str) -> bool:
    return name.lower().startswith("note_")


def clear_main_statement_dirs(year_dir: Path) -> None:
    """Drop previously captured main-statement pages for this year."""
    if not year_dir.is_dir():
        return
    for child in year_dir.iterdir():
        if child.is_dir() and not _is_note_dir(child.name):
            shutil.rmtree(child, ignore_errors=True)
        elif child.is_file() and child.name == INDEX_NAME:
            try:
                child.unlink()
            except OSError:
                pass


def write_capture_index(
    year_dir: Path,
    *,
    company: str,
    period: str,
    year: int,
) -> dict:
    """Rewrite index.json from whatever image files are currently in the year folder."""
    period_name = "Quarterly" if str(period).strip().lower().startswith("q") else "Annual"
    statements: dict[str, list[str]] = {}
    if year_dir.is_dir():
        for child in sorted(year_dir.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            files = sorted(
                (
                    p.name
                    for p in child.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                ),
                key=lambda name: name.lower(),
            )
            if files:
                statements[child.name] = files
    payload = {
        "company": company,
        "period": period_name,
        "year": int(year),
        "statements": statements,
    }
    year_dir.mkdir(parents=True, exist_ok=True)
    (year_dir / INDEX_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def save_pipeline_captures(
    *,
    company: str,
    period: str,
    year: int,
    src_caps: Path,
) -> int:
    """Copy a run's capture folder into the fixed company/period/year tree.

    Also creates the sibling Annual and Quarterly folders so a new company
    matches the report layout even when only one period was extracted.
    """
    if not src_caps.is_dir():
        return 0
    dest = year_capture_dir(company, period, year)
    company_dir = dest.parent.parent
    (company_dir / "Annual").mkdir(parents=True, exist_ok=True)
    (company_dir / "Quarterly").mkdir(parents=True, exist_ok=True)
    copied = install_capture_tree(dest, src_caps)
    if copied:
        write_capture_index(dest, company=company, period=period, year=year)
    return copied


def install_capture_tree(year_dir: Path, src_caps: Path) -> int:
    """Replace this year's main-statement images with ``src_caps/<statement>/*.png``."""
    if not src_caps.is_dir():
        return 0

    grouped: list[tuple[Path, list[Path]]] = []
    for stmt_dir in src_caps.iterdir():
        if not stmt_dir.is_dir():
            continue
        pngs = sorted(
            p for p in stmt_dir.iterdir()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS
        )
        if pngs:
            grouped.append((stmt_dir, pngs))
    if not grouped:
        return 0

    clear_main_statement_dirs(year_dir)
    year_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for stmt_dir, pngs in grouped:
        dest_dir = year_dir / stmt_dir.name
        if dest_dir.exists():
            shutil.rmtree(dest_dir, ignore_errors=True)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for png in pngs:
            shutil.copy2(png, dest_dir / png.name)
            copied += 1
    return copied


def install_rendered_statements(
    year_dir: Path,
    images: dict[str, list[tuple[str, bytes]]],
) -> int:
    """Replace this year's main-statement images with freshly rendered PNG bytes."""
    usable = {
        key: blobs
        for key, blobs in images.items()
        if key and not _is_note_dir(key) and blobs
    }
    if not usable:
        return 0

    clear_main_statement_dirs(year_dir)
    year_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for key, blobs in usable.items():
        dest_dir = year_dir / key
        dest_dir.mkdir(parents=True, exist_ok=True)
        seen: set[str] = set()
        for name, data in blobs:
            if name in seen:
                continue
            seen.add(name)
            (dest_dir / name).write_bytes(data)
            written += 1
    return written


def rebuild_indexes(root: Path | None = None) -> int:
    """Write index.json for every existing Company/Period/Year folder."""
    base = root or DEMO_CAPTURES_OUT
    if not base.is_dir():
        return 0
    count = 0
    for company_dir in base.iterdir():
        if not company_dir.is_dir():
            continue
        for period_dir in company_dir.iterdir():
            if not period_dir.is_dir():
                continue
            if period_dir.name not in ("Annual", "Quarterly"):
                continue
            for year_dir in period_dir.iterdir():
                if not year_dir.is_dir() or not year_dir.name.isdigit():
                    continue
                write_capture_index(
                    year_dir,
                    company=company_dir.name,
                    period=period_dir.name,
                    year=int(year_dir.name),
                )
                count += 1
    return count
