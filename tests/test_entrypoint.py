"""Unit tests for the GDR entry point outside a GDB process."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import rtthread.commands as commands
import rtthread.version as version
from gdr.layout import KernelLayout
from rtthread.layout import RtConfig


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


def test_setup_rtthread_passes_the_parsed_version_to_layout_builder(monkeypatch):
    """The layout factory receives the selected RT-Thread compatibility profile."""
    entrypoint = _load_entrypoint()
    received: list[tuple[int, int, int]] = []
    monkeypatch.setattr(commands, "is_initialized", lambda: False)
    monkeypatch.setattr(version, "check_version", lambda _value: (3, 1, 3))
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)
    monkeypatch.setattr("rtthread.layout.detect_config", RtConfig)
    monkeypatch.setattr(
        "rtthread.layout.build_layouts",
        lambda _config, target_version: (
            received.append(target_version) or KernelLayout()
        ),
    )
    monkeypatch.setattr("rtthread.adapter.register_adapter", lambda _layout: None)
    monkeypatch.setattr("rtthread.commands.register_commands", lambda _layout: None)
    monkeypatch.setattr(entrypoint, "register_printers", lambda _layout: None)

    entrypoint._setup_rtthread("3.1.3")

    assert received == [(3, 1, 3)]
