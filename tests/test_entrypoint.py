"""Unit tests for the GDR entry point outside a GDB process."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import rtthread.version as version
from freertos.config import FreeRtosConfig
from freertos.layout import FreeRtosLayout
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
    monkeypatch.setattr("gdr.registry.is_initialized", lambda: True)
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
    monkeypatch.setattr("gdr.registry.is_initialized", lambda: False)
    monkeypatch.setattr(version, "check_version", lambda _value: (3, 1, 3))
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)
    monkeypatch.setattr("rtthread.layout.detect_config", RtConfig)
    monkeypatch.setattr(
        "rtthread.layout.build_layouts",
        lambda _config, target_version: (
            received.append(target_version) or KernelLayout()
        ),
    )
    monkeypatch.setattr("gdr.functions.register_functions", lambda: None)
    monkeypatch.setattr("rtthread.commands.register_commands", lambda: None)
    monkeypatch.setattr("gdr.registry.register", lambda _adapter: None)
    monkeypatch.setattr(entrypoint, "register_printers", lambda _layout: None)

    entrypoint._setup_rtthread("3.1.3")

    assert received == [(3, 1, 3)]


@pytest.mark.parametrize(
    "failure_stage", ["adapter", "printers", "functions", "commands"]
)
def test_setup_rtthread_commits_only_after_all_registration_steps(
    monkeypatch, failure_stage
):
    """Every failed setup stage leaves the registry empty and can be retried."""
    entrypoint = _load_entrypoint()
    attempts: dict[str, int] = {}
    registrations: list[object] = []
    adapter_instance = object()

    def step(name: str):
        attempts[name] = attempts.get(name, 0) + 1
        if name == failure_stage and attempts[name] == 1:
            raise RuntimeError(f"{name} registration interrupted")

    monkeypatch.setattr("gdr.registry.is_initialized", lambda: bool(registrations))
    monkeypatch.setattr("gdr.registry.register", registrations.append)
    monkeypatch.setattr(version, "check_version", lambda _value: (4, 0, 5))
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)
    monkeypatch.setattr("rtthread.layout.detect_config", RtConfig)
    monkeypatch.setattr(
        "rtthread.layout.build_layouts",
        lambda _config, _version: KernelLayout(),
    )
    monkeypatch.setattr(
        "rtthread.adapter.RtThreadAdapter",
        lambda _layout: step("adapter") or adapter_instance,
    )
    monkeypatch.setattr(
        entrypoint, "register_printers", lambda _layout: step("printers")
    )
    monkeypatch.setattr("gdr.functions.register_functions", lambda: step("functions"))
    monkeypatch.setattr("rtthread.commands.register_commands", lambda: step("commands"))

    with pytest.raises(RuntimeError, match=f"{failure_stage} registration interrupted"):
        entrypoint._setup_rtthread("4.0.5")

    assert registrations == []

    entrypoint._setup_rtthread("4.0.5")

    assert registrations == [adapter_instance]


def test_setup_freertos_activates_adapter_last(monkeypatch):
    """FreeRTOS also remains inactive until all GDB registrations succeed."""
    entrypoint = _load_entrypoint()
    events: list[object] = []
    adapter_instance = object()
    layout = FreeRtosLayout(config=FreeRtosConfig(), version=(10, 3, 1))

    monkeypatch.setattr("gdr.registry.is_initialized", lambda: False)
    monkeypatch.setattr(
        "gdr.registry.register", lambda selected: events.append(("active", selected))
    )
    monkeypatch.setattr("freertos.version.check_version", lambda _value: (10, 3, 1))
    monkeypatch.setattr("freertos.config.detect_config", FreeRtosConfig)
    monkeypatch.setattr(
        "freertos.layout.build_layout", lambda _config, _version: layout
    )
    monkeypatch.setattr(
        "freertos.adapter.FreeRtosAdapter",
        lambda _layout: events.append("adapter") or adapter_instance,
    )
    monkeypatch.setattr(
        "gdr.functions.register_functions", lambda: events.append("functions")
    )
    monkeypatch.setattr(
        "freertos.commands.register_commands", lambda: events.append("commands")
    )
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)

    entrypoint._setup_freertos("10.3.1")

    assert events == [
        "adapter",
        "functions",
        "commands",
        ("active", adapter_instance),
    ]
