"""Regression tests for the RTOS-agnostic core boundary."""

from __future__ import annotations

import re
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent / "gdr"
_RTTHREAD_TERMS = re.compile(
    r"\b(?:rt-?thread|rtthread|rt_[a-z0-9_]+)\b", re.IGNORECASE
)


def test_core_source_contains_no_rtthread_coupling():
    """Target names, symbols, and types must remain in the adapter package."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        match = _RTTHREAD_TERMS.search(source.read_text())
        if match is not None:
            violations.append(f"{source.name}: {match.group(0)}")

    assert not violations, "RT-Thread coupling leaked into gdr/: " + ", ".join(
        violations
    )
