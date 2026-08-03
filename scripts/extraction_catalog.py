"""Build extraction keyword catalog from COMB model manifest + alias maps."""
from __future__ import annotations

from typing import Any

from comb_manifest import load_manifest
from generate_comb_model import (
    DRIVERS_LABEL_ALIASES,
    LABEL_ALIASES,
    QuarterlyExtractor,
)


def _default_aliases(label: str) -> list[str]:
    return [label.strip().lower()]


def merge_manifest_labels(
    base_aliases: dict[str, list[str]],
    manifest_labels: list[str],
) -> dict[str, list[str]]:
    """Add xlsx row labels missing from alias map (exact match; casing preserved)."""
    merged: dict[str, list[str]] = {k: list(v) for k, v in base_aliases.items()}

    for raw in manifest_labels:
        label = str(raw).strip()
        if not label or label in merged:
            continue
        merged[label] = _default_aliases(label)

    return merged


def manifest_labels_for_scope(scope: str, manifest: dict[str, Any]) -> list[str]:
    # Topic / section headers are never description-extraction keywords.
    topic_norms = {
        "less expenses",
        "income statement",
        "oci",
        "balance sheet",
        "cash flow statement",
        "assets",
        "liabilities",
        "equity",
        "memorandum information",
        "adjustments for",
        "profit attributable to",
        "earnings per share",
    }

    def _keep(label: str) -> bool:
        from generate_comb_model import norm_label

        nl = norm_label(label)
        if nl in topic_norms:
            return False
        if nl.startswith("cash flows from ") and nl.endswith(" activities"):
            return False
        return True

    if scope == "fs":
        return [
            str(r["label"])
            for r in manifest.get("fs", {}).get("rows", [])
            if r.get("kind") == "data" and _keep(str(r.get("label") or ""))
        ]
    if scope == "drivers":
        return [
            str(r["label"])
            for r in manifest.get("drivers", {}).get("rows", [])
            if r.get("kind") == "data" and _keep(str(r.get("label") or ""))
        ]
    if scope == "quarterly":
        return [
            str(l)
            for l in manifest.get("quarterly", {}).get("labels", [])
            if _keep(str(l))
        ]
    return []


def build_catalog(*, refresh_manifest: bool = False) -> dict[str, list[dict[str, Any]]]:
    manifest = load_manifest(refresh=refresh_manifest)
    scopes = {
        "fs": LABEL_ALIASES,
        "drivers": DRIVERS_LABEL_ALIASES,
        "quarterly": QuarterlyExtractor.QUARTERLY_LABEL_ALIASES,
    }
    payload: dict[str, list[dict[str, Any]]] = {}
    for scope, base in scopes.items():
        merged = merge_manifest_labels(base, manifest_labels_for_scope(scope, manifest))
        payload[scope] = [
            {
                "label": label,
                "aliases": list(aliases),
                "alias_count": len(aliases),
            }
            for label, aliases in sorted(merged.items())
        ]
    return payload
