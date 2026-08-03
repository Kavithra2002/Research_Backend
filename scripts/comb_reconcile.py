"""Reconciliation helpers for COMB driver note groups."""
from __future__ import annotations

from typing import Any


def parse_number(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "")
    if not s or s in {"-", "—", "N/A", "n/a"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        n = float(s)
        return -n if neg else n
    except ValueError:
        return None


def sum_values(values: list[float | None]) -> float | None:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums)


def reconcile_group(
    parent_value: float | None,
    child_values: list[float | None],
    *,
    tolerance_pct: float = 0.02,
) -> dict[str, Any]:
    """Check children sum ≈ parent."""
    child_sum = sum_values(child_values)
    if parent_value is None:
        return {
            "ok": False,
            "reason": "parent_missing",
            "child_sum": child_sum,
            "parent": parent_value,
        }
    if child_sum is None:
        return {
            "ok": False,
            "reason": "children_missing",
            "child_sum": None,
            "parent": parent_value,
        }
    diff = abs(child_sum - parent_value)
    tol = max(abs(parent_value) * tolerance_pct, 1000.0)
    return {
        "ok": diff <= tol,
        "reason": "ok" if diff <= tol else "sum_mismatch",
        "child_sum": child_sum,
        "parent": parent_value,
        "diff": diff,
    }
