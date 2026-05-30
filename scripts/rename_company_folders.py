"""
rename_company_folders.py
=========================
Rename the placeholder `reports/companyN` folders to their actual company
names by extracting the entity name (PLC / Limited / Bank …) from each
folder's annual-report PDF.

Strategy
--------
1. For every sub-folder under ``reports/``, pick the most-recent PDF from
   ``Annual/`` (else any PDF under the folder).
2. Use PyMuPDF (fitz) — falls back to pdfplumber — to extract text from the
   first 20 pages.
3. Run a regex that matches entity names ending in PLC, Limited, Ltd or
   Bank, score them by frequency and length, pick the strongest hit.
4. Clean up the name (strip noise like "About", "At", normalise spaces,
   handle the � / replacement char that pdfplumber sometimes returns for
   diacritics).
5. Produce a Windows-safe folder name in Title_Case_With_Underscores form
   and resolve collisions by appending `_2`, `_3`, …

Usage
-----
    # Dry-run (default) — prints the proposed mapping, changes nothing.
    python rename_company_folders.py

    # Actually rename the folders.
    python rename_company_folders.py --apply

    # Write the mapping to a JSON file (always written, even on dry-run).
    python rename_company_folders.py --map-out rename_map.json
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout so company names with diacritics print cleanly on Windows.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import fitz  # PyMuPDF — primary text extractor
except Exception:
    fitz = None

try:
    import pdfplumber  # fallback
except Exception:
    pdfplumber = None


SCRIPT_DIR  = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
REPORTS_DIR = BACKEND_DIR / "reports"

PDF_EXT = (".pdf", ".PDF")

# Companies whose folders should not be renamed even if a candidate is found.
PROTECTED_NAMES = {"a. janashakthi finace"}   # lowercase comparison

# Pages to scan per PDF. Most cover pages + ToC + chairman's review fall in here.
MAX_PAGES_SCAN = 20


# ─────────────────────────────────────────────────────────────────────────────
# Name extraction
# ─────────────────────────────────────────────────────────────────────────────

# Two-pass regex strategy:
#   Pass 1 — high-priority suffixes (PLC / Limited / Ltd).  Run first so that
#            "HATTON NATIONAL BANK PLC" is captured in full instead of being
#            cut off at "BANK".
#   Pass 2 — Bank-suffix fallback.  Only used when pass 1 finds nothing.
#
# `_NAME_CHARS` accepts Unicode letters (\w), digits, common punctuation in
# company names, and U+FFFD (the replacement char pdfplumber emits for unmapped
# glyphs, e.g. the ã in "Amãna Bank PLC").  We use \w with re.UNICODE so
# accented letters such as ã, é, ç are accepted.
_NAME_CHARS = r"\w&.\-'()\ufffd "

ENTITY_RE_PLC = re.compile(
    rf"\b("
    rf"[A-Z][{_NAME_CHARS}]{{2,80}}?"
    rf"\s+(?:PLC|P\.L\.C\.|Limited|LIMITED|Ltd\.?|LTD\.?|"
    rf"\(Pvt\)\s*Ltd|\(Private\)\s*Limited)"
    rf")\b",
    re.UNICODE,
)

ENTITY_RE_BANK = re.compile(
    rf"\b([A-Z][{_NAME_CHARS}]{{2,80}}?\s+(?:Bank|BANK))\b",
    re.UNICODE,
)

# Words that show up immediately before a company name and should be stripped.
_LEADING_STRIP = re.compile(
    r"^(?:about|at|of|by|for|the|this|our|to|in|on|from|with|"
    r"chairman\s+of|chief\s+executive\s+officer\s+of|director\s+of|"
    r"affairs\s+of|annual\s+report\s+of)\s+",
    re.I,
)

# Generic phrases that masquerade as company names; drop these candidates.
_NOISE_NAMES = {
    "bank plc", "company plc", "holdings plc", "finance plc", "limited",
    "ltd", "plc", "private limited", "pvt ltd", "p.l.c.",
    "the company plc", "investment bank",
}


def _clean_candidate(name: str) -> str:
    """Normalise whitespace, drop leading filler words, fix common artefacts."""
    s = re.sub(r"\s+", " ", (name or "").strip())
    # Strip leading filler ("At Hemas Holdings PLC" -> "Hemas Holdings PLC").
    while True:
        new = _LEADING_STRIP.sub("", s)
        if new == s:
            break
        s = new
    # pdfplumber / PyMuPDF emit U+FFFD for unmapped glyphs (e.g. Amãna →
    # Am\ufffd na).  Drop the replacement char and collapse any double spaces
    # it leaves behind so the folder name is ASCII-clean.
    s = s.replace("\ufffd", "")
    s = re.sub(r"\s+", " ", s).strip(" -.,:;|")
    return s


def _score_candidate(name: str, freq: int) -> tuple[int, int, int]:
    """Higher tuple wins.

    Priorities, in order:
      1. Suffix rank (PLC > Limited/Ltd > Bank)
      2. Frequency
      3. Length (prefer "Hatton National Bank PLC" over "Hatton National Bank")
    """
    upper = name.upper()
    if re.search(r"\bPLC\b|\bP\.L\.C\.\b", upper):
        suffix_rank = 3
    elif re.search(r"\bLIMITED\b|\bLTD\b", upper):
        suffix_rank = 2
    elif re.search(r"\bBANK\b", upper):
        suffix_rank = 1
    else:
        suffix_rank = 0
    return (suffix_rank, freq, len(name))


def _extract_text_pages(pdf_path: Path, max_pages: int) -> list[str]:
    """Return text for up to `max_pages` pages, preferring PyMuPDF."""
    pages: list[str] = []
    if fitz is not None:
        try:
            doc = fitz.open(pdf_path)
            try:
                for i in range(min(max_pages, len(doc))):
                    try:
                        pages.append(doc[i].get_text() or "")
                    except Exception:
                        pages.append("")
            finally:
                doc.close()
            return pages
        except Exception:
            pages = []
    if pdfplumber is not None:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for i in range(min(max_pages, len(pdf.pages))):
                    try:
                        pages.append(pdf.pages[i].extract_text() or "")
                    except Exception:
                        pages.append("")
        except Exception:
            pass
    return pages


def _collect_matches(pages: list[str], pattern: re.Pattern) -> Counter[str]:
    freq: Counter[str] = Counter()
    for txt in pages:
        if not txt:
            continue
        for raw in pattern.findall(txt):
            name = _clean_candidate(raw)
            if not name or len(name) < 6 or len(name) > 80:
                continue
            if name.lower() in _NOISE_NAMES:
                continue
            # Skip candidates with no real first word (only the entity suffix
            # survived after stripping leading fillers).
            head = name.split()[0].lower()
            if head in {"plc", "limited", "ltd", "bank", "company", "holdings"}:
                continue
            freq[name] += 1
    return freq


def extract_company_name(pdf_path: Path) -> tuple[str | None, list[tuple[str, int]]]:
    """Return (best_name, [(name, freq), …]) for the given PDF.

    `best_name` is None when no plausible candidate was found (e.g. fully
    scanned PDFs with no text layer).
    """
    pages = _extract_text_pages(pdf_path, MAX_PAGES_SCAN)
    if not any(pages):
        return None, []

    # Pass 1: PLC / Limited / Ltd — these are the canonical entity suffixes.
    freq = _collect_matches(pages, ENTITY_RE_PLC)
    # Pass 2 (fallback): bare "X Bank" — used only when no PLC/Limited match.
    if not freq:
        freq = _collect_matches(pages, ENTITY_RE_BANK)

    if not freq:
        return None, []

    ranked = sorted(
        freq.items(), key=lambda kv: _score_candidate(kv[0], kv[1]), reverse=True
    )
    return ranked[0][0], ranked


# ─────────────────────────────────────────────────────────────────────────────
# Folder helpers
# ─────────────────────────────────────────────────────────────────────────────

_WIN_INVALID = re.compile(r'[\\/:*?"<>|]+')


def folder_name_for(company: str) -> str:
    """Convert 'Amãna Bank PLC' → 'Amana_Bank_PLC'.

    Diacritics are stripped via NFKD decomposition so the resulting folder
    name is ASCII-only and safe on any filesystem.
    """
    # Decompose accented characters and drop the combining marks (ã → a).
    s = unicodedata.normalize("NFKD", company)
    s = "".join(c for c in s if not unicodedata.combining(c))
    # Replace anything still outside ASCII with empty so the path stays clean.
    s = s.encode("ascii", "ignore").decode("ascii")
    s = _WIN_INVALID.sub("", s)
    s = re.sub(r"\s+", "_", s.strip())
    s = s.strip("._")
    return s or "company"


def _natural_sort_key(name: str):
    parts = re.split(r"(\d+)", name)
    return [int(p) if p.isdigit() else p.lower() for p in parts]


def _pick_pdf(company_dir: Path) -> Path | None:
    annual = company_dir / "Annual"
    if annual.is_dir():
        pdfs = [p for p in annual.iterdir() if p.suffix in PDF_EXT]
        if pdfs:
            return max(pdfs, key=lambda p: p.stat().st_mtime)
    direct = [p for p in company_dir.iterdir() if p.is_file() and p.suffix in PDF_EXT]
    if direct:
        return max(direct, key=lambda p: p.stat().st_mtime)
    for p in company_dir.rglob("*.pdf"):
        return p
    for p in company_dir.rglob("*.PDF"):
        return p
    return None


def _resolve_collision(target: Path, taken: set[str]) -> Path:
    """If `target` already exists (on disk or in the current batch), append _2, _3…"""
    base_name = target.name
    parent = target.parent
    if base_name.lower() not in taken and not target.exists():
        taken.add(base_name.lower())
        return target
    n = 2
    while True:
        candidate = parent / f"{base_name}_{n}"
        if candidate.name.lower() not in taken and not candidate.exists():
            taken.add(candidate.name.lower())
            return candidate
        n += 1


# ─────────────────────────────────────────────────────────────────────────────
# Main driver
# ─────────────────────────────────────────────────────────────────────────────

def build_rename_plan(reports_dir: Path) -> list[dict]:
    """Return one entry per company folder with proposed rename info."""
    plan: list[dict] = []
    taken: set[str] = set()

    # Reserve folder names that already exist (and aren't being renamed) so we
    # never collide with them.
    existing = {p.name.lower() for p in reports_dir.iterdir() if p.is_dir()}

    folders = sorted(
        [p for p in reports_dir.iterdir() if p.is_dir()],
        key=lambda p: _natural_sort_key(p.name),
    )

    for folder in folders:
        entry: dict = {
            "current_name": folder.name,
            "pdf": None,
            "proposed_name": None,
            "proposed_folder": None,
            "candidates": [],
            "action": "skip",
            "reason": "",
        }

        if folder.name.lower() in PROTECTED_NAMES:
            entry["reason"] = "protected — already has a real name"
            plan.append(entry)
            continue

        pdf = _pick_pdf(folder)
        if pdf is None:
            entry["reason"] = "no PDF found"
            plan.append(entry)
            continue
        entry["pdf"] = pdf.name

        name, ranked = extract_company_name(pdf)
        entry["candidates"] = [
            {"name": n, "freq": f} for n, f in ranked[:5]
        ]

        if not name:
            entry["reason"] = (
                "no text layer (likely scanned PDF) — rename manually"
            )
            plan.append(entry)
            continue

        proposed_folder = folder_name_for(name)
        # Don't rename if the proposed folder name is identical to current.
        if proposed_folder.lower() == folder.name.lower():
            entry["proposed_name"] = name
            entry["proposed_folder"] = proposed_folder
            entry["reason"] = "already matches proposed name"
            plan.append(entry)
            continue

        # Don't collide with an existing-and-not-being-renamed folder.
        target = folder.parent / proposed_folder
        # Pretend the current folder is "free" so we don't get blocked by it.
        taken_view = taken | (existing - {folder.name.lower()})
        # Re-implement collision resolution using the merged set.
        if (target.name.lower() not in taken_view) and (not target.exists()):
            taken_view.add(target.name.lower())
            final = target
        else:
            n = 2
            while True:
                cand = target.parent / f"{target.name}_{n}"
                if cand.name.lower() not in taken_view and not cand.exists():
                    taken_view.add(cand.name.lower())
                    final = cand
                    break
                n += 1
        taken.add(final.name.lower())

        entry["proposed_name"] = name
        entry["proposed_folder"] = final.name
        entry["action"] = "rename"
        plan.append(entry)

    return plan


def print_plan(plan: list[dict]) -> None:
    print()
    print("=" * 78)
    print(f"  Rename plan ({len(plan)} folders)")
    print("=" * 78)
    width = max((len(e["current_name"]) for e in plan), default=20)
    for e in plan:
        cur = e["current_name"].ljust(width)
        if e["action"] == "rename":
            print(f"  RENAME   {cur}  ->  {e['proposed_folder']}")
            print(f"           extracted: {e['proposed_name']!r}")
        else:
            tag = e["reason"] or "no change"
            if e.get("proposed_folder"):
                print(f"  KEEP     {cur}      ({tag})")
                print(f"           extracted: {e['proposed_name']!r}")
            else:
                print(f"  SKIP     {cur}      ({tag})")
        if e["candidates"]:
            top = ", ".join(f"{c['name']!r}×{c['freq']}" for c in e["candidates"][:3])
            print(f"           top hits: {top}")
    print()


def apply_plan(plan: list[dict], reports_dir: Path) -> list[dict]:
    """Perform the actual renames. Returns the log entries with `done` status."""
    log: list[dict] = []
    for e in plan:
        rec = dict(e)
        if e["action"] != "rename":
            rec["done"] = False
            log.append(rec)
            continue
        src = reports_dir / e["current_name"]
        dst = reports_dir / e["proposed_folder"]
        try:
            src.rename(dst)
            rec["done"] = True
            print(f"  renamed  {e['current_name']}  ->  {e['proposed_folder']}")
        except Exception as ex:
            rec["done"] = False
            rec["error"] = str(ex)
            print(f"  FAILED   {e['current_name']}: {ex}")
        log.append(rec)
    return log


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reports", type=Path, default=REPORTS_DIR,
                    help=f"Folder containing per-company sub-folders "
                         f"(default: {REPORTS_DIR})")
    ap.add_argument("--apply", action="store_true",
                    help="Actually rename the folders (default is dry-run)")
    ap.add_argument("--map-out", type=Path,
                    default=BACKEND_DIR / "rename_map.json",
                    help="Write the rename mapping to this JSON file "
                         "(default: rename_map.json next to this script)")
    args = ap.parse_args()

    reports = args.reports.resolve()
    if not reports.is_dir():
        print(f"ERROR: reports folder not found: {reports}")
        sys.exit(1)

    if fitz is None and pdfplumber is None:
        print("ERROR: neither PyMuPDF (fitz) nor pdfplumber is installed.")
        sys.exit(1)

    print(f"Scanning {reports} …")
    plan = build_rename_plan(reports)
    print_plan(plan)

    rename_count = sum(1 for e in plan if e["action"] == "rename")
    skip_count   = len(plan) - rename_count

    mapping = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reports_dir": str(reports),
        "applied": bool(args.apply),
        "entries": plan,
    }

    if args.apply:
        print(f"Applying {rename_count} renames …")
        log = apply_plan(plan, reports)
        mapping["entries"] = log
    else:
        print(f"DRY-RUN: would rename {rename_count} folder(s), "
              f"skip {skip_count}.")
        print("        Re-run with --apply to perform the rename.")

    if args.map_out:
        args.map_out.parent.mkdir(parents=True, exist_ok=True)
        args.map_out.write_text(
            json.dumps(mapping, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"Wrote mapping -> {args.map_out}")


if __name__ == "__main__":
    main()
