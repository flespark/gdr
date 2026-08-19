"""Regression tests for the RTOS-agnostic core boundary."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[3] / "gdr"
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


def test_core_never_imports_an_rtos_adapter_package():
    """Only the composition root may select RT-Thread or FreeRTOS."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            violations.extend(
                f"{source.name}:{node.lineno}: {name}"
                for name in names
                if name.split(".", 1)[0] in {"rtthread", "freertos"}
            )
    assert not violations, "RTOS adapter imported by gdr/: " + ", ".join(violations)


def test_production_files_stay_within_the_size_limit():
    """The repository keeps production modules below the 1500-line limit."""
    roots = [
        Path(__file__).resolve().parents[3] / "gdr",
        Path(__file__).resolve().parents[3] / "rtthread",
        Path(__file__).resolve().parents[3] / "freertos",
    ]
    oversized = [
        str(path.relative_to(roots[0].parent))
        for root in roots
        for path in root.glob("*.py")
        if len(path.read_text().splitlines()) > 1500
    ]
    assert not oversized
