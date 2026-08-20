"""
Release-grade FS cell confirmation: extract → rule check → AI confirm.

High-risk / missing / rule-failed cells are confirmed from a cropped PDF row
image via OpenAI vision (optional second independent confirm). Values that
already pass rules are left alone — PDF/AI must not overwrite trusted cells.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

try:
    import fitz
except ImportError:
    fitz = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from comb_annual_fs_pdf_verify import (
    FS_RATIO_LABELS,
    FS_SMALL_VALUE_LABELS,
    annual_values_match,
    is_suspicious_fs_value,
    is_year_like_amount,
    _fitz_search_needles,
    _search_terms_for_fs_label,
)
from comb_annual_fs_validate import (
    FS_TAX_ON_SERVICES,
    OP_PROFIT_AFTER_FS_TAX,
    OP_PROFIT_BEFORE_FS_TAX,
    reconcile_operating_profit_fs_tax,
)
from comb_annual_memorandum import is_memorandum_fs_label
from comb_note_extractor import labels_polarity_conflict, norm_label, resolve_annual_pdf
from comb_note_openai import DEFAULT_MODEL, openai_chat_token_kwargs, resolve_api_key
from comb_reconcile import parse_number

RISK_TRUSTED = "trusted"
RISK_UNCERTAIN = "uncertain"
RISK_MISSING = "missing"
RISK_FAILED = "failed"

# Always AI-confirm these even when rules look fine (known hard rows).
ALWAYS_HIGH_RISK_LABELS = frozenset(
    {
        norm_label("Non-controlling interest"),
        norm_label("Equity holders of the Bank"),
        norm_label(OP_PROFIT_BEFORE_FS_TAX),
        norm_label(OP_PROFIT_AFTER_FS_TAX),
        norm_label(FS_TAX_ON_SERVICES),
        norm_label("Profit for the year"),
        norm_label("Profit before tax"),
        norm_label("Share of profit/(loss) of associate, net of tax"),
        norm_label("Basic earnings per ordinary share (Rs.)"),
        norm_label("Diluted earnings per ordinary share (Rs.)"),
        norm_label("Diviend per share"),
    }
)


def _parse_json_content(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def classify_fs_cell_risk(
    label: str,
    value: float | None,
    values: dict[str, float | None],
    *,
    year: int,
    pdf_disagreed: bool = False,
) -> str:
    """Classify one FS cell for the release confirmation gate."""
    nl = norm_label(label)
    if value is None:
        return RISK_MISSING
    if is_year_like_amount(value, report_year=year):
        return RISK_FAILED
    if is_suspicious_fs_value(label, value, values, report_year=year):
        return RISK_FAILED

    # before / after tax identity
    if nl == norm_label(OP_PROFIT_AFTER_FS_TAX):
        before = values.get(OP_PROFIT_BEFORE_FS_TAX)
        tax = values.get(FS_TAX_ON_SERVICES)
        if before is not None and tax is not None:
            expected = float(before) - float(tax)
            if abs(float(tax)) >= 1.0 and annual_values_match(value, before):
                return RISK_FAILED
            if not annual_values_match(value, expected):
                return RISK_FAILED

    # Polarity sibling clone
    for peer_lbl, peer_val in values.items():
        if peer_val is None or peer_lbl == label:
            continue
        if labels_polarity_conflict(label, peer_lbl) and annual_values_match(
            value, peer_val
        ):
            return RISK_FAILED

    if pdf_disagreed:
        return RISK_UNCERTAIN
    if nl in ALWAYS_HIGH_RISK_LABELS:
        return RISK_UNCERTAIN
    # Memo / ratio / per-share rows are confirmed only when missing, failed,
    # or PDF-disagreed — not on every successful extract.
    return RISK_TRUSTED


def collect_high_risk_labels(
    labels: list[str],
    values: dict[str, float | None],
    *,
    year: int,
    pdf_mismatch_labels: set[str] | None = None,
) -> list[str]:
    pdf_mismatch_labels = pdf_mismatch_labels or set()
    out: list[str] = []
    for lbl in labels:
        risk = classify_fs_cell_risk(
            lbl,
            values.get(lbl),
            values,
            year=year,
            pdf_disagreed=lbl in pdf_mismatch_labels,
        )
        if risk in {RISK_MISSING, RISK_UNCERTAIN, RISK_FAILED}:
            out.append(lbl)
    return out


def _find_label_hit(
    page: Any,
    label: str,
) -> Any | None:
    terms = _search_terms_for_fs_label(label)
    for term in terms:
        for needle in _fitz_search_needles(term):
            try:
                hits = page.search_for(needle) or []
            except Exception:
                hits = []
            if not hits:
                soft = re.sub(r"[\/\-_]+", " ", needle)
                soft = re.sub(r"\s+", " ", soft).strip()
                if soft != needle:
                    try:
                        hits = page.search_for(soft) or []
                    except Exception:
                        hits = []
            if hits:
                # Prefer the left-most / top-most description hit.
                return sorted(hits, key=lambda r: (float(r.y0), float(r.x0)))[0]
    return None


def render_fs_row_crop_png(
    pdf_path: Path,
    label: str,
    *,
    page_hint: list[int] | None = None,
    dpi: int = 180,
    pad_x: float = 8.0,
    pad_y: float = 10.0,
    row_height: float = 28.0,
) -> tuple[bytes | None, int | None]:
    """Crop a single statement row (label + amount columns) as PNG bytes."""
    if fitz is None:
        return None, None
    with fitz.open(str(pdf_path)) as doc:
        page_idxs = (
            [p - 1 for p in page_hint if isinstance(p, int) and 1 <= p <= len(doc)]
            if page_hint
            else list(range(len(doc)))
        )
        for pi in page_idxs:
            page = doc[pi]
            hit = _find_label_hit(page, label)
            if hit is None:
                continue
            page_rect = page.rect
            y0 = max(0.0, float(hit.y0) - pad_y)
            y1 = min(float(page_rect.y1), max(float(hit.y1), float(hit.y0) + row_height) + pad_y)
            # Full-width strip so GROUP/BANK year columns stay visible.
            x0 = max(0.0, 20.0)
            x1 = float(page_rect.x1) - pad_x
            clip = fitz.Rect(x0, y0, x1, y1)
            scale = dpi / 72.0
            pix = page.get_pixmap(
                matrix=fitz.Matrix(scale, scale),
                clip=clip,
                colorspace=fitz.csRGB,
                alpha=False,
            )
            return pix.tobytes("png"), pi + 1
    return None, None


def _ai_read_row_value(
    client: Any,
    *,
    png_bytes: bytes,
    label: str,
    year: int,
    entity_column: str,
    model: str,
    pass_name: str,
) -> float | None:
    entity = (entity_column or "group").upper()
    prompt = f"""You are verifying ONE row from a Sri Lankan bank annual report Income Statement / SoFP / Cash Flow table.

Target row label (must match this row, not a sibling):
"{label}"

Read ONLY the **{entity}** column for calendar year **{year}** (Rs. '000).
Do NOT use BANK if entity is GROUP. Do NOT use {year - 1}.

Critical:
- "before" and "after" are different rows — do not mix them
- "Equity holders of the Bank" is NOT "Non-controlling interest"
- Parentheses = negative; dash/blank = null

Return ONLY JSON:
{{"label": "{label}", "entity": "{entity}", "year": {year}, "value": <number or null>, "pass": "{pass_name}"}}
"""
    content = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
            },
        },
        {"type": "text", "text": prompt},
    ]
    for attempt in range(1, 3):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_object"},
                **openai_chat_token_kwargs(model, max_tokens=256),
            )
            payload = _parse_json_content(resp.choices[0].message.content or "{}")
            return parse_number(payload.get("value"))
        except Exception:
            if attempt < 2:
                time.sleep(1.5 * attempt)
    return None


def ai_confirm_fs_labels(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    pdf_index=None,
    pdf_mismatch_labels: set[str] | None = None,
    double_confirm: bool = True,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    entity_column: str = "group",
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """
    Step J — AI-confirm high-risk FS cells from cropped PDF row images.

    Trusted cells are never overwritten. AI values are accepted only when:
      - first vision pass returns a usable number, and
      - (if double_confirm) a second independent pass agrees, or
      - the AI value restores a broken rule identity (before - tax = after).
    """
    log = log_fn or (lambda msg: print(msg, flush=True))
    stats: dict[str, Any] = {
        "enabled": False,
        "candidates": 0,
        "confirmed": 0,
        "corrected": 0,
        "filled": 0,
        "rejected": 0,
        "failed": 0,
        "details": [],
        "confidence": {},
    }

    api_key = api_key or resolve_api_key()
    if OpenAI is None or fitz is None or not api_key:
        log("  [annual-validate] Step J — AI confirm skipped (no API / vision deps)")
        return values, stats

    pdf_path = resolve_annual_pdf(db, company_slug, year)
    if not pdf_path or not Path(pdf_path).exists():
        log("  [annual-validate] Step J — AI confirm skipped (no annual PDF)")
        return values, stats

    candidates = collect_high_risk_labels(
        labels,
        values,
        year=year,
        pdf_mismatch_labels=pdf_mismatch_labels,
    )
    stats["enabled"] = True
    stats["candidates"] = len(candidates)
    if not candidates:
        log("  [annual-validate] Step J — AI confirm: no high-risk cells")
        for lbl in labels:
            if values.get(lbl) is not None:
                stats["confidence"][lbl] = RISK_TRUSTED
        return values, stats

    log(
        f"  [annual-validate] Step J — AI confirm {len(candidates)} high-risk FS cell(s)"
        f"{' (double-check)' if double_confirm else ''}"
    )
    client = OpenAI(api_key=api_key)

    pages_by_section: dict[str, list[int]] = {}
    if pdf_index is not None:
        pages_by_section = getattr(pdf_index, "pages_by_section", {}) or {}

    for lbl in candidates:
        section = fs_sections.get(lbl)
        page_hint = None
        if section and pages_by_section.get(section):
            page_hint = list(pages_by_section[section])
        elif pages_by_section.get("income_statement"):
            page_hint = list(pages_by_section["income_statement"])

        png, page_num = render_fs_row_crop_png(
            Path(pdf_path), lbl, page_hint=page_hint
        )
        if not png:
            # Fall back to whole-section pages without a tight crop.
            stats["failed"] += 1
            stats["details"].append(
                {"label": lbl, "ok": False, "reason": "row_crop_not_found"}
            )
            log(f"    AI confirm failed {lbl!r}: row crop not found")
            continue

        v1 = _ai_read_row_value(
            client,
            png_bytes=png,
            label=lbl,
            year=year,
            entity_column=entity_column,
            model=model,
            pass_name="pass1",
        )
        if v1 is None or is_year_like_amount(v1, report_year=year):
            stats["failed"] += 1
            stats["details"].append(
                {"label": lbl, "ok": False, "reason": "ai_null", "page": page_num}
            )
            log(f"    AI confirm failed {lbl!r}: no usable value (page {page_num})")
            continue

        accepted = v1
        if double_confirm:
            v2 = _ai_read_row_value(
                client,
                png_bytes=png,
                label=lbl,
                year=year,
                entity_column=entity_column,
                model=model,
                pass_name="pass2",
            )
            if v2 is None or not annual_values_match(v1, v2):
                # Allow single-pass accept only when it repairs a known identity.
                current = values.get(lbl)
                repairs_after = (
                    norm_label(lbl) == norm_label(OP_PROFIT_AFTER_FS_TAX)
                    and values.get(OP_PROFIT_BEFORE_FS_TAX) is not None
                    and values.get(FS_TAX_ON_SERVICES) is not None
                    and annual_values_match(
                        v1,
                        float(values[OP_PROFIT_BEFORE_FS_TAX])
                        - float(values[FS_TAX_ON_SERVICES]),
                    )
                )
                if not repairs_after:
                    stats["rejected"] += 1
                    stats["details"].append(
                        {
                            "label": lbl,
                            "ok": False,
                            "reason": "double_confirm_mismatch",
                            "pass1": v1,
                            "pass2": v2,
                            "page": page_num,
                        }
                    )
                    log(
                        f"    AI confirm rejected {lbl!r}: "
                        f"pass1={v1} pass2={v2} (page {page_num})"
                    )
                    continue
                accepted = v1

        current = values.get(lbl)
        current_bad = (
            current is None
            or is_suspicious_fs_value(
                lbl, current, values, report_year=year
            )
            or classify_fs_cell_risk(lbl, current, values, year=year) == RISK_FAILED
        )

        if is_suspicious_fs_value(lbl, accepted, {**values, lbl: accepted}, report_year=year):
            stats["rejected"] += 1
            stats["details"].append(
                {
                    "label": lbl,
                    "ok": False,
                    "reason": "ai_value_suspicious",
                    "value": accepted,
                    "page": page_num,
                }
            )
            log(f"    AI confirm rejected {lbl!r}: suspicious value {accepted}")
            continue

        if current is None:
            values[lbl] = accepted
            stats["filled"] += 1
            stats["confirmed"] += 1
            stats["confidence"][lbl] = "ai_confirmed"
            log(f"    AI filled {lbl!r} = {accepted:,.4g} (page {page_num})")
        elif annual_values_match(current, accepted):
            stats["confirmed"] += 1
            stats["confidence"][lbl] = "ai_confirmed"
            log(f"    AI confirmed {lbl!r} = {accepted:,.4g} (page {page_num})")
        elif current_bad:
            values[lbl] = accepted
            stats["corrected"] += 1
            stats["confirmed"] += 1
            stats["confidence"][lbl] = "ai_confirmed"
            log(
                f"    AI corrected {lbl!r}: {current:,.4g} -> {accepted:,.4g} "
                f"(page {page_num})"
            )
        else:
            # Plausible extracted value already present — do not let a bad crop
            # overwrite it (2019 op-profit before: 30.2M kept vs AI 29.5M).
            stats["rejected"] += 1
            stats["confidence"][lbl] = "trusted"
            stats["details"].append(
                {
                    "label": lbl,
                    "ok": False,
                    "reason": "kept_plausible_current",
                    "current": current,
                    "ai": accepted,
                    "page": page_num,
                }
            )
            log(
                f"    AI disagreed {lbl!r}: kept {current:,.4g} "
                f"(AI={accepted:,.4g}, page {page_num})"
            )
            continue

        stats["details"].append(
            {
                "label": lbl,
                "ok": True,
                "from": current,
                "to": values.get(lbl),
                "page": page_num,
            }
        )

    # Mark remaining filled cells as rule-trusted when not touched.
    for lbl in labels:
        if lbl not in stats["confidence"] and values.get(lbl) is not None:
            risk = classify_fs_cell_risk(lbl, values.get(lbl), values, year=year)
            stats["confidence"][lbl] = (
                RISK_TRUSTED if risk == RISK_TRUSTED else "unconfirmed"
            )

    values, tax_stats = reconcile_operating_profit_fs_tax(values, log_fn=log)
    stats["fs_tax_check"] = tax_stats
    return values, stats


def run_release_fs_validation_gate(
    db,
    company_slug: str,
    year: int,
    labels: list[str],
    values: dict[str, float | None],
    fs_sections: dict[str, str],
    *,
    pdf_index=None,
    pdf_mismatch_labels: set[str] | None = None,
    use_ai_confirm: bool = True,
    double_confirm: bool = True,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    """Rule check + optional AI confirm (release gate)."""
    log = log_fn or (lambda msg: print(msg, flush=True))
    values, tax_stats = reconcile_operating_profit_fs_tax(values, log_fn=log)
    gate: dict[str, Any] = {"fs_tax_check": tax_stats}

    if not use_ai_confirm:
        gate["ai_confirm"] = {"enabled": False, "skipped": True}
        return values, gate

    values, ai_stats = ai_confirm_fs_labels(
        db,
        company_slug,
        year,
        labels,
        values,
        fs_sections,
        pdf_index=pdf_index,
        pdf_mismatch_labels=pdf_mismatch_labels,
        double_confirm=double_confirm,
        log_fn=log,
    )
    gate["ai_confirm"] = ai_stats
    return values, gate
