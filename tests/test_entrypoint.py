"""Unit tests for the GDR entry point outside a GDB process."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_entrypoint():
    """Load ``gdr.py`` without executing its GDB-only script entry point."""
    path = Path(__file__).resolve().parent.parent / "gdr.py"
    spec = importlib.util.spec_from_file_location("gdr_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_args_retains_environment_initialization(monkeypatch):
    """Non-interactive launchers configure the RTOS through environment vars."""
    monkeypatch.setenv("GDR_RTOS", "rtthread")
    monkeypatch.setenv("GDR_VERSION", "4.0.5")

    assert _load_entrypoint()._parse_args() == {"rtos": "rtthread", "version": "4.0.5"}
