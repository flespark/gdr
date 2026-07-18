"""Unit tests for the GDR entry point outside a GDB process."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import rtthread.commands as commands
import rtthread.version as version


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


def test_setup_rtthread_skips_an_existing_initialization(monkeypatch):
    """A repeated init must not reprobe or replace the active RT-Thread layout."""
    entrypoint = _load_entrypoint()
    warnings: list[str] = []
    version_checks: list[str] = []
    monkeypatch.setattr(commands, "is_initialized", lambda: True)
    monkeypatch.setattr(version, "check_version", version_checks.append)
    monkeypatch.setattr(entrypoint, "warn", warnings.append)

    entrypoint._setup_rtthread("4.1.1")

    assert version_checks == []
    assert warnings == [
        "RT-Thread support is already initialized; restart GDB before "
        "selecting a different target or version"
    ]
