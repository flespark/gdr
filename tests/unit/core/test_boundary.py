"""Regression tests for the RTOS-agnostic core boundary."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[3] / "gdr"
_RTTHREAD_TERMS = re.compile(
    r"\b(?:rt-?thread|rtthread|rt_[a-z0-9_]+)\b", re.IGNORECASE
)
# FreeRTOS-specific identifiers and symbols must also stay out of the core.
# ``tcb`` alone is excluded: it is too generic to prove coupling, and the
# FreeRTOS adapter already owns the struct by its ``struct tskTaskControlBlock``
# name (matched below via the ``tsk`` prefix term).
# Reason: the ``tsk`` prefix and the kernel ``*_t`` typedefs are matched with an
# identifier tail rather than a bare ``\b``, so long names such as
# ``tskKERNEL_VERSION_NUMBER`` are caught and not only 4-character words.
_FREERTOS_TERMS = re.compile(
    r"\b(?:free-?rtos|freertos|frt_[a-z0-9_]+|tsk[A-Za-z0-9_]+"
    r"|Queue_t|TCB_t|List_t|ListItem_t|MiniListItem_t|StreamBuffer_t)\b",
    re.IGNORECASE,
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


def test_core_source_contains_no_freertos_coupling():
    """FreeRTOS symbol/type names must remain in the adapter package."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        match = _FREERTOS_TERMS.search(source.read_text())
        if match is not None:
            violations.append(f"{source.name}: {match.group(0)}")

    assert not violations, "FreeRTOS coupling leaked into gdr/: " + ", ".join(
        violations
    )


def test_freertos_term_pattern_catches_long_identifiers():
    """The FreeRTOS term pattern must not silently match nothing.

    A trailing ``\\b`` after a fixed-width class (``tsk[A-Z]``) only matches
    4-character words, which would let ``tskKERNEL_VERSION_NUMBER`` or
    ``tskTaskControlBlock`` leak into the core unnoticed.
    """
    for term in (
        "tskTaskControlBlock",
        "tskKERNEL_VERSION_NUMBER",
        "FreeRTOS",
        "Queue_t",
        "TCB_t",
    ):
        assert _FREERTOS_TERMS.search(f"a {term} b") is not None, term
    for benign in ("task_count", "structs", "list_items"):
        assert _FREERTOS_TERMS.search(benign) is None, benign


def test_core_never_imports_an_rtos_adapter_package():
    """Only the composition root may select RT-Thread or FreeRTOS."""
    violations = []
    for source in _CORE_DIR.glob("*.py"):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.Import or ast.ImportFrom)):
                lineno = node.lineno
            else:
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif node.module:
                names = [node.module]
            violations.extend(
                f"{source.name}:{lineno}: {name}"
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
