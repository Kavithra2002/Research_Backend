"""
extraction_accuracy.py
======================
Shared accuracy gate for annual / quarterly extraction.

After a first extract (OpenAI vision or local), re-check each statement for:
  • layout / column misalignment (labels vs amounts vs Note)
  • empty / missing required statements
  • sparse data rows

When issues are found and capture images exist, re-send those pages to OpenAI.
Keep-best merge: a later pass replaces the first only when it scores higher
(more usable rows). Empty / parse-failed retries never wipe a good first
extract. No endless force-retry — one accuracy round by default.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

import extraction_validation as val

LogFn = Callable[[str, str], None]

ANNUAL_KEYS = ("income_statement", "oci", "sofp", "cash_flows")
_NOTE_REF = re.compile(r"^\d{1,2}(?:\.\d{1,2})?$")
_COMMA_AMT = re.compile(r"\(?-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?")


def _noop_log(message: str, level: str = "info") -> None:
    print(f"  [accuracy] {message}")


def resolve_api_key(cli_key: str | None = None) -> str | None:
    import os

    if cli_key and str(cli_key).strip():
        return str(cli_key).strip()
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ["OPENAI_API_KEY"].strip() or None
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return None
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == "OPENAI_API_KEY":
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                return v or None
    except Exception:
        return None
    return None


def _row_count(entry: dict[str, Any] | None) -> int:
    if not isinstance(entry, dict):
        return 0
    # step3 shape
    data = entry.get("data")
    if isinstance(data, dict):
        tables = data.get("tables") or []
        total = 0
        for tbl in tables:
            if isinstance(tbl, dict):
                total += len(tbl.get("rows") or [])
        return total
    # printed shape
    return len(entry.get("rows") or [])


def statement_quality_score(entry: dict[str, Any] | None) -> int:
    """Higher = better usable table. Parse failures / empty → 0 or negative.

    Used by keep-best merge: never replace a good first extract with a later
    empty/parse-failed attempt; only replace when the new result scores higher.
    """
    if not isinstance(entry, dict):
        return -1
    status = str(entry.get("status") or "").lower()
    if status in {
        "parse_failed",
        "api_error",
        "no_images",
        "error",
        "failed",
    }:
        return -1
    if entry.get("ok") is False and _row_count(entry) < 4:
        return -1
    data = entry.get("data")
    if isinstance(data, dict) and str(data.get("status") or "").lower() in {
        "parse_failed",
        "api_error",
    }:
        return -1
    rows = _row_count(entry)
    if rows <= 0:
        return 0
    # Prefer real tables with headers; slight bonus so a fuller parse wins.
    bonus = 0
    if isinstance(data, dict):
        tables = data.get("tables") or []
        if tables and isinstance(tables[0], dict) and (tables[0].get("header_rows") or []):
            bonus = 2
    elif entry.get("header_rows"):
        bonus = 2
    return rows + bonus


def prefer_better_entry(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    *,
    key: str = "",
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Keep the first good extract; replace only when the new one is clearly better."""
    _log = log or _noop_log
    old = existing if isinstance(existing, dict) else None
    new = incoming if isinstance(incoming, dict) else None
    if new is None:
        return dict(old or {})
    if old is None:
        return dict(new)

    old_s = statement_quality_score(old)
    new_s = statement_quality_score(new)
    label = key or str(old.get("key") or new.get("key") or "statement")

    if new_s > old_s:
        _log(
            f"[keep-best] {label}: replaced prior (score {old_s}) with better "
            f"(score {new_s})"
        )
        return dict(new)

    if old_s > new_s:
        _log(
            f"[keep-best] {label}: kept prior (score {old_s}); "
            f"newer was worse/empty (score {new_s})"
        )
        return dict(old)

    # Equal scores — keep the first (do not thrash on ties).
    if old_s > 0:
        _log(f"[keep-best] {label}: kept prior (tie score {old_s})")
        return dict(old)
    return dict(new)


def merge_results_keep_best(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
    *,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Merge statement maps with keep-best per key (never wipe good with empty)."""
    out: dict[str, Any] = dict(existing or {})
    for key, entry in (incoming or {}).items():
        if not isinstance(entry, dict):
            continue
        prior = out.get(key) if isinstance(out.get(key), dict) else None
        out[str(key)] = prefer_better_entry(prior, entry, key=str(key), log=log)
    return out


def _is_ok_entry(entry: dict[str, Any] | None) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("ok") is False:
        return False
    status = entry.get("status")
    if status is not None and status != "ok":
        return False
    if statement_quality_score(entry) < 4:
        return False
    return _row_count(entry) >= 4


def openai_entry_to_printed(
    key: str,
    entry: dict[str, Any],
    *,
    title: str | None = None,
    pages: list[int] | None = None,
) -> dict[str, Any]:
    """Convert one step3/OpenAI statement into printed_statements shape."""
    display = title or str(entry.get("title") or key)
    if not _is_ok_entry(entry):
        return {
            "key": key,
            "title": display,
            "pages": pages or [],
            "header_rows": [],
            "rows": [],
            "note_column": None,
            "unit": "",
            "ok": False,
        }
    data = entry.get("data") if isinstance(entry.get("data"), dict) else entry
    tables = (data or {}).get("tables") or []
    table0 = tables[0] if tables and isinstance(tables[0], dict) else {}
    header_rows = table0.get("header_rows") or []
    rows_in = table0.get("rows") or []
    rows: list[dict[str, Any]] = []
    note_hits = 0
    for row in rows_in:
        if not isinstance(row, dict):
            continue
        cells = [("" if c is None else str(c)) for c in (row.get("cells") or [])]
        note_ref = None
        if len(cells) > 1 and _NOTE_REF.fullmatch(cells[1].strip()):
            note_ref = cells[1].strip()
            note_hits += 1
        rows.append(
            {
                "cells": cells,
                "note_ref": note_ref,
                "style": str(row.get("style") or "data"),
            }
        )
    return {
        "key": key,
        "title": str((data or {}).get("statement_title") or display),
        "pages": pages or [],
        "header_rows": header_rows,
        "rows": rows,
        "note_column": 1 if note_hits >= 2 else None,
        "unit": str((data or {}).get("unit") or ""),
        "ok": len(rows) >= 4,
        "extraction_model": "openai-vision",
    }


def openai_results_to_printed_payload(
    results: dict[str, Any],
    *,
    company_slug: str,
    company_name: str,
    year: int,
    source_pdf: str,
    pages_by_key: dict[str, list[int]] | None = None,
    titles: dict[str, str] | None = None,
) -> dict[str, Any]:
    pages_by_key = pages_by_key or {}
    titles = titles or {}
    statements: list[dict[str, Any]] = []
    for key in ANNUAL_KEYS:
        entry = results.get(key) if isinstance(results, dict) else None
        statements.append(
            openai_entry_to_printed(
                key,
                entry if isinstance(entry, dict) else {},
                title=titles.get(key),
                pages=pages_by_key.get(key) or [],
            )
        )
    units = [s.get("unit") for s in statements if s.get("unit")]
    return {
        "company_slug": company_slug,
        "company_name": company_name,
        "year": year,
        "source_pdf": source_pdf,
        "unit": units[0] if units else "",
        "statements": statements,
        "notes": {},
        "extraction_model": "openai-vision",
    }


def statement_needs_reextract(
    statement: dict[str, Any],
    *,
    report_year: int | None = None,
) -> tuple[bool, list[str]]:
    """Return (needs_fix, reasons) for one printed or OpenAI-shaped statement."""
    reasons: list[str] = []
    key = str(statement.get("key") or "statement")

    # Normalize OpenAI shape into printed-like for layout checks.
    if isinstance(statement.get("data"), dict) and "rows" not in statement:
        printed = openai_entry_to_printed(key, statement)
    else:
        printed = statement

    if not printed.get("ok", True) and _row_count(printed) < 4:
        reasons.append(f"{key}: missing or empty")
        return True, reasons

    layout = val.validate_printed_layout(printed, report_year=report_year)
    if not layout.ok:
        reasons.extend(layout.notes or [f"{key}: layout failed"])
        return True, reasons

    data_rows = [
        r
        for r in (printed.get("rows") or [])
        if isinstance(r, dict) and (r.get("style") or "data").lower() == "data"
    ]
    if len(data_rows) < 5:
        reasons.append(f"{key}: too few data rows ({len(data_rows)})")
        return True, reasons

    # Label column must be text, not amounts (classic left-shift garble).
    label_is_amount = 0
    label_has_text = 0
    for row in data_rows:
        cells = row.get("cells") or []
        lab = str(cells[0] if cells else "").strip()
        if not lab:
            continue
        if _COMMA_AMT.fullmatch(lab) or (
            lab.startswith("(") and _COMMA_AMT.search(lab)
        ):
            label_is_amount += 1
        elif re.search(r"[A-Za-z]{3,}", lab):
            label_has_text += 1
    if data_rows and label_is_amount >= max(3, len(data_rows) // 3) and label_has_text < 3:
        reasons.append(
            f"{key}: row labels look like amounts ({label_is_amount} rows) — "
            "columns misaligned / missing structure"
        )
        return True, reasons

    # Amount density: data rows should usually carry at least one amount.
    with_amt = 0
    for row in data_rows:
        cells = row.get("cells") or []
        if any(_COMMA_AMT.search(str(c or "")) for c in cells[1:]):
            with_amt += 1
    if data_rows and with_amt / len(data_rows) < 0.4:
        reasons.append(
            f"{key}: only {with_amt}/{len(data_rows)} data rows have amounts"
        )
        return True, reasons

    return False, []


def keys_needing_reextract(
    results_or_statements: Any,
    *,
    report_year: int | None = None,
    required_keys: tuple[str, ...] = ANNUAL_KEYS,
) -> dict[str, list[str]]:
    """Map statement_key → reasons for keys that fail the accuracy gate."""
    needs: dict[str, list[str]] = {}
    if isinstance(results_or_statements, dict) and "statements" not in [
        k for k in results_or_statements if k in required_keys
    ]:
        # Could be step3 map {income_statement: {...}} OR printed payload
        if any(k in results_or_statements for k in required_keys):
            source = results_or_statements
            for key in required_keys:
                entry = source.get(key)
                if entry is None:
                    needs[key] = [f"{key}: not extracted"]
                    continue
                bad, reasons = statement_needs_reextract(
                    {"key": key, **(entry if isinstance(entry, dict) else {})},
                    report_year=report_year,
                )
                if bad:
                    needs[key] = reasons
            return needs

    if isinstance(results_or_statements, dict) and isinstance(
        results_or_statements.get("statements"), list
    ):
        by_key = {
            str(s.get("key")): s
            for s in results_or_statements["statements"]
            if isinstance(s, dict) and s.get("key")
        }
        for key in required_keys:
            entry = by_key.get(key)
            if entry is None:
                needs[key] = [f"{key}: not extracted"]
                continue
            bad, reasons = statement_needs_reextract(entry, report_year=report_year)
            if bad:
                needs[key] = reasons
        return needs

    if isinstance(results_or_statements, dict):
        # quarterly wrapper: statements map
        stmts = results_or_statements.get("statements")
        if isinstance(stmts, dict):
            for key, entry in stmts.items():
                if not isinstance(entry, dict):
                    continue
                bad, reasons = statement_needs_reextract(
                    {"key": key, **entry}, report_year=report_year
                )
                if bad:
                    needs[str(key)] = reasons
    return needs


def reextract_annual_keys_with_openai(
    *,
    keys: list[str],
    manifest_path: Path,
    captures_dir: Path,
    api_key: str,
    model: str = "gpt-5",
    existing_results: dict[str, Any] | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Re-send specific statement captures to OpenAI and merge into results."""
    _log = log or _noop_log
    import step3_send_to_openai as step3

    if not keys:
        return dict(existing_results or {})

    _log(f"Re-extracting {len(keys)} statement(s) via OpenAI for accuracy: {', '.join(keys)}")
    prior = dict(existing_results or {})
    merged = step3.run_statements(
        manifest_path=manifest_path,
        captures_dir=captures_dir,
        api_key=api_key,
        keys=keys,
        model=model,
        dry_run=False,
        out_dir=None,
        existing_results=prior,
        write_outputs=False,
        keep_best=True,
        keep_best_log=_log,
    )
    if not isinstance(merged, dict):
        return prior
    # Defensive: keep-best again in case step3 was an older build.
    return merge_results_keep_best(prior, merged, log=_log)


def accuracy_pass_annual(
    *,
    results: dict[str, Any],
    manifest_path: Path | None,
    captures_dir: Path | None,
    api_key: str | None,
    model: str = "gpt-5",
    report_year: int | None = None,
    max_rounds: int | None = None,
    log: LogFn | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Run up to ``max_rounds`` accuracy gates. Returns (results, report_dict).

    Default is one recheck round (re-extract only failing keys) so a report
    stays in the ~2–3 minute window without skipping accuracy.
    """
    if max_rounds is None:
        try:
            from extraction_timing import DEFAULT_ACCURACY_ROUNDS

            max_rounds = DEFAULT_ACCURACY_ROUNDS
        except Exception:
            max_rounds = 1
    max_rounds = max(1, int(max_rounds))
    _log = log or _noop_log
    current = dict(results or {})
    report: dict[str, Any] = {
        "ok": True,
        "rounds": 0,
        "reextracted": [],
        "remaining": {},
        "notes": [],
    }

    if not api_key or not manifest_path or not captures_dir:
        needs = keys_needing_reextract(current, report_year=report_year)
        report["remaining"] = needs
        report["ok"] = len(needs) == 0
        if needs:
            report["notes"].append(
                "Accuracy issues found but OpenAI re-extract unavailable "
                "(missing API key or captures)."
            )
            for key, reasons in needs.items():
                for r in reasons:
                    _log(r, "warn")
        return current, report

    for round_i in range(1, max_rounds + 1):
        needs = keys_needing_reextract(current, report_year=report_year)
        if not needs:
            report["ok"] = True
            report["rounds"] = round_i - 1
            _log("Accuracy gate passed — values/layout look consistent")
            return current, report

        report["rounds"] = round_i
        for key, reasons in needs.items():
            for r in reasons:
                _log(f"[round {round_i}] {r}", "warn")

        keys = sorted(needs.keys())
        # Only retry keys that have capture images on disk.
        retryable = [
            k
            for k in keys
            if (captures_dir / k).is_dir()
            and any((captures_dir / k).glob("page_*.png"))
        ]
        skipped = [k for k in keys if k not in retryable]
        if skipped:
            _log(
                f"[round {round_i}] no captures for: {', '.join(skipped)} — cannot re-extract",
                "warn",
            )
        if not retryable:
            report["remaining"] = needs
            report["ok"] = False
            break

        current = reextract_annual_keys_with_openai(
            keys=retryable,
            manifest_path=manifest_path,
            captures_dir=captures_dir,
            api_key=api_key,
            model=model,
            existing_results=current,
            log=_log,
        )
        report["reextracted"].extend(retryable)

    final_needs = keys_needing_reextract(current, report_year=report_year)
    report["remaining"] = final_needs
    report["ok"] = len(final_needs) == 0
    if final_needs:
        _log(
            f"Accuracy gate still has gaps after {report['rounds']} round(s): "
            f"{', '.join(final_needs)}",
            "warn",
        )
    else:
        _log("Accuracy gate passed after re-extract")
    return current, report


def accuracy_pass_printed_payload(
    payload: dict[str, Any],
    *,
    api_key: str | None = None,
    model: str = "gpt-5",
    manifest_path: Path | None = None,
    captures_dir: Path | None = None,
    openai_results: dict[str, Any] | None = None,
    log: LogFn | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accuracy gate for a printed_statements payload; optionally repair via OpenAI."""
    _log = log or _noop_log
    year = payload.get("year")
    try:
        report_year = int(year) if year is not None else None
    except (TypeError, ValueError):
        report_year = None

    needs = keys_needing_reextract(payload, report_year=report_year)
    if not needs:
        _log("Printed layout accuracy gate passed")
        return payload, {"ok": True, "reextracted": [], "remaining": {}, "notes": []}

    for key, reasons in needs.items():
        for r in reasons:
            _log(r, "warn")

    if not (api_key and manifest_path and captures_dir and openai_results is not None):
        return payload, {
            "ok": False,
            "reextracted": [],
            "remaining": needs,
            "notes": ["Cannot re-extract — missing OpenAI key or captures"],
        }

    fixed_results, report = accuracy_pass_annual(
        results=openai_results,
        manifest_path=manifest_path,
        captures_dir=captures_dir,
        api_key=api_key,
        model=model,
        report_year=report_year,
        log=_log,
    )
    pages_by_key: dict[str, list[int]] = {}
    titles: dict[str, str] = {}
    for stmt in payload.get("statements") or []:
        if isinstance(stmt, dict) and stmt.get("key"):
            pages_by_key[str(stmt["key"])] = list(stmt.get("pages") or [])
            titles[str(stmt["key"])] = str(stmt.get("title") or stmt["key"])

    new_payload = openai_results_to_printed_payload(
        fixed_results,
        company_slug=str(payload.get("company_slug") or ""),
        company_name=str(payload.get("company_name") or ""),
        year=int(report_year or 0),
        source_pdf=str(payload.get("source_pdf") or ""),
        pages_by_key=pages_by_key,
        titles=titles,
    )
    new_payload["notes"] = payload.get("notes") or {}
    new_payload["validation"] = payload.get("validation")
    new_payload["accuracy"] = report
    return new_payload, report
