"""
extraction_timing.py
====================
Shared timing / throughput rules for annual + quarterly extractors.

Goals
-----
  • Finish one report in about 2–3 minutes (hard ceiling ~3 min when possible).
  • Never “speed-cheat” under ~1 minute by skipping capture quality or the
    accuracy recheck — wrong values in the wrong columns are worse than waiting.
  • Prefer parallelism + smarter PDF scans over lowering DPI or dropping OpenAI.
"""

from __future__ import annotations

import time
from typing import Callable

# Soft target / ceiling for a single report (seconds).
TARGET_SECONDS = 150          # ~2.5 min typical aim
MAX_SECONDS = 180             # ~3 min ceiling

# Do not skip accuracy just to finish under this.
MIN_ACCURACY_BUDGET_SECONDS = 60

# One accuracy gate after extract (re-send only failing keys).
DEFAULT_ACCURACY_ROUNDS = 1

# Parallel OpenAI / render workers for the 4 main FS statements.
OPENAI_PARALLEL_WORKERS = 4
CAPTURE_PARALLEL_WORKERS = 4

# Main FS keys used for early-stop in step1 / option-1 captures.
CORE_FS_KEYS = (
    "income_statement",
    "oci",
    "sofp",
    "cash_flows",
)


def elapsed_s(t0: float) -> float:
    return max(0.0, time.monotonic() - t0)


def log_timing(label: str, t0: float, log: Callable[[str], None] | None = None) -> float:
    secs = elapsed_s(t0)
    msg = f"[timing] {label}: {secs:.1f}s"
    if log:
        log(msg)
    else:
        print(f"  {msg}")
    return secs
