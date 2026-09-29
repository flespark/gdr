"""Unit tests for the GDR entry point outside a GDB process."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import rtthread.version as version
from freertos.layout import FreeRtosConfig, FreeRtosLayout
from gdr.layout import KernelLayout
from rtthread.layout import RtConfig


def _load_entrypoint():
    """Load ``gdr.py`` without executing its GDB-only script entry point."""
    path = Path(__file__).resolve().parents[3] / "gdr.py"
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
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: True)
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
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)
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
    monkeypatch.setattr("gdr.functions.register_api_functions", lambda: None)
    monkeypatch.setattr("rtthread.commands.register_commands", lambda: None)
    monkeypatch.setattr("gdr.adapter_api.register", lambda _adapter, **_kwargs: None)
    monkeypatch.setattr(entrypoint, "register_printers", lambda _layout: None)

    entrypoint._setup_rtthread("3.1.3")

    assert received == [(3, 1, 3)]


@pytest.mark.parametrize(
    "failure_stage", ["adapter", "printers", "functions", "api_functions", "commands"]
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

    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: bool(registrations))
    monkeypatch.setattr(
        "gdr.adapter_api.register",
        lambda adapter, **_kwargs: registrations.append(adapter),
    )
    monkeypatch.setattr(version, "check_version", lambda _value: (4, 0, 5))
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)
    monkeypatch.setattr("rtthread.layout.detect_config", RtConfig)
    monkeypatch.setattr(
        "rtthread.layout.build_layouts",
        lambda _config, _version: KernelLayout(),
    )
    monkeypatch.setattr(
        "rtthread.adapter.RtThreadAdapter",
        lambda _layout, **_kwargs: step("adapter") or adapter_instance,
    )
    monkeypatch.setattr(
        entrypoint, "register_printers", lambda _layout: step("printers")
    )
    monkeypatch.setattr("gdr.functions.register_functions", lambda: step("functions"))
    monkeypatch.setattr(
        "gdr.functions.register_api_functions", lambda: step("api_functions")
    )
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

    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)
    monkeypatch.setattr(
        "gdr.adapter_api.register",
        lambda selected, **_kwargs: events.append(("active", selected)),
    )
    monkeypatch.setattr("freertos.version.check_version", lambda _value: (10, 3, 1))
    monkeypatch.setattr("freertos.layout.detect_config", FreeRtosConfig)
    monkeypatch.setattr(
        "freertos.layout.build_layout", lambda _config, _version: layout
    )
    monkeypatch.setattr(
        "freertos.adapter.FreeRtosAdapter",
        lambda _layout: events.append("adapter") or adapter_instance,
    )
    monkeypatch.setattr(
        entrypoint, "register_printers", lambda _layout: events.append("printers")
    )
    monkeypatch.setattr(
        "gdr.functions.register_functions", lambda: events.append("functions")
    )
    monkeypatch.setattr(
        "gdr.functions.register_api_functions", lambda: events.append("api_functions")
    )
    monkeypatch.setattr(
        "freertos.commands.register_commands", lambda: events.append("commands")
    )
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)

    entrypoint._setup_freertos("10.3.1")

    assert events == [
        "adapter",
        "printers",
        "functions",
        "api_functions",
        "commands",
        ("active", adapter_instance),
    ]


def test_invoke_command_guards_setup_errors(monkeypatch):
    """An unexpected ``gdr init`` failure surfaces as an error, never a traceback."""
    entrypoint = _load_entrypoint()
    from gdr import gdb_bridge as bridge

    errors: list[str] = []

    def _broken_setup(*_args):
        raise ValueError("broken layout probe")

    monkeypatch.setattr(entrypoint, "_setup_rtos", _broken_setup)
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    assert entrypoint._invoke_command("init rtthread 4.0.5") is None
    assert errors == ["_invoke_command: ValueError: broken layout probe"]


def test_invoke_command_passes_unsupported_rtos_to_setup(monkeypatch):
    """The dispatch reaches ``_setup_rtos`` which itself reports unsupported names."""
    entrypoint = _load_entrypoint()
    received: list[list[str]] = []

    def _record(rtos: str, version: str):
        received.append([rtos, version])

    monkeypatch.setattr(entrypoint, "_setup_rtos", _record)
    entrypoint._invoke_command("init rtthread 4.0.5")
    assert received == [["rtthread", "4.0.5"]]


def test_root_help_supports_topics_and_completion(capsys):
    entrypoint = _load_entrypoint()

    entrypoint._invoke_command("help init")
    output = capsys.readouterr().out

    assert "DESCRIPTION" in output
    assert "CONFIGURATION" in output
    assert "LIMITATIONS" in output

    entrypoint._invoke_command("help")
    overview = capsys.readouterr().out
    assert "AVAILABLE COMMAND" in overview
    assert "Executable `gdr` subcommands" in overview
    assert "RTOS COMMAND TREE" in overview
    assert "run `rtt help` or `frt help`" in overview
    assert "HELP TOPIC" in overview
    assert "not an executable GDB subcommand" in overview
    assert "GUIDES" not in overview
    assert "pretty-printers" in overview

    entrypoint._invoke_command("help pretty-printers")
    printers = capsys.readouterr().out
    assert "RTOS-neutral" in printers
    assert "rtt help pretty-printers" in printers
    assert "frt help pretty-printers" in printers
    assert "SUBTOPICS" not in printers
    assert "tskTaskControlBlock" not in printers

    assert entrypoint._complete("help pre", "pre") == ["pretty-printers"]
    assert "init" in entrypoint._complete("help ", None)


def test_invalid_root_command_prints_only_one_usage_warning(monkeypatch, capsys):
    entrypoint = _load_entrypoint()
    warnings: list[str] = []
    monkeypatch.setattr(entrypoint, "warn", warnings.append)

    entrypoint._invoke_command("pretty-printers timer")

    assert warnings == ["usage: gdr init <rtos> <version> (run 'gdr help' for help)"]
    assert capsys.readouterr().out == ""


def test_api_command_writes_a_json_file(monkeypatch, tmp_path):
    """``gdr api --out`` writes the envelope a plugin reads back."""
    import json

    entrypoint = _load_entrypoint()
    notes: list[str] = []
    monkeypatch.setattr(entrypoint, "info", notes.append)
    monkeypatch.setattr(
        "gdr.api.envelope_json",
        lambda _verb, *_args: '{"ok":true,"verb":"tasks"}',
    )
    out = tmp_path / "tasks.json"

    entrypoint._api_command(["tasks", "--out", str(out)])

    assert json.loads(out.read_text(encoding="utf-8"))["ok"] is True
    assert notes and "wrote" in notes[0]


def test_init_json_reports_a_failed_setup(monkeypatch, capsys):
    """``gdr init --json`` is one structured line, not log scraping."""
    import json

    entrypoint = _load_entrypoint()
    monkeypatch.setattr(entrypoint, "_setup_rtos", lambda _rtos, _version: None)
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)

    entrypoint._init_command(["rtthread", "4.0.5", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["rtos"] == "rtthread"
    assert "error" in result


def test_init_force_resets_before_replacing(monkeypatch):
    """``--force`` drops the active adapter instead of refusing."""
    entrypoint = _load_entrypoint()
    calls: list[str] = []
    monkeypatch.setattr(entrypoint, "_reset_command", lambda: calls.append("reset"))
    monkeypatch.setattr(
        entrypoint, "_setup_rtos", lambda _rtos, _version: calls.append("setup")
    )
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: True)

    entrypoint._init_command(["freertos", "10.3.1", "--force"])

    assert calls == ["reset", "setup"]


def test_probe_command_renders_the_evidence_table(monkeypatch):
    """The human view lists each candidate's evidence and what resolved."""
    entrypoint = _load_entrypoint()
    tables: list[tuple[list[list[str]], list[str]]] = []
    notes: list[str] = []
    monkeypatch.setattr(entrypoint, "info", notes.append)
    monkeypatch.setattr(entrypoint, "warn", notes.append)
    monkeypatch.setattr(
        entrypoint,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )
    monkeypatch.setattr(
        "gdr.api.api_call",
        lambda _verb, *_args: {
            "ok": True,
            "data": {
                "candidates": [
                    {
                        "rtos": "rtthread",
                        "version": "4.0.5",
                        "confidence": "exact",
                        "evidence": ["symbol: rt_thread_self"],
                        "detail": "",
                    }
                ],
                "resolved": {"rtos": "rtthread", "version": "4.0.5"},
            },
        },
    )

    entrypoint._probe_command([])

    assert tables == [
        (
            [["rtthread", "4.0.5", "exact", "symbol: rt_thread_self"]],
            ["RTOS", "Version", "Confidence", "Evidence"],
        )
    ]
    assert any("resolved: rtthread 4.0.5" in note for note in notes)


def test_probe_command_json_is_the_api_envelope(monkeypatch):
    """``--json`` is the same payload a client gets from the verb."""
    entrypoint = _load_entrypoint()
    written: list[dict] = []
    envelope = {"schema_version": 1, "ok": True, "data": {"candidates": []}}
    monkeypatch.setattr(entrypoint, "_write_json", written.append)
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)
    monkeypatch.setattr("gdr.api.api_call", lambda _verb, *_args: envelope)

    entrypoint._probe_command(["--json"])

    assert written == [envelope]

    warnings: list[str] = []
    monkeypatch.setattr(entrypoint, "warn", warnings.append)
    entrypoint._probe_command(["surprise"])
    assert warnings == ["usage: gdr probe [--json]"]


def test_resolve_auto_accepts_only_one_exact_candidate(monkeypatch):
    """``auto`` is not a guess: a range or an ambiguity must reach the user."""
    from gdr.probe import ProbeCandidate

    entrypoint = _load_entrypoint()
    cases = {
        "exact": [ProbeCandidate("rtthread", "exact", version="4.0.5")],
        "range": [ProbeCandidate("rtthread", "range")],
        "undetected": [ProbeCandidate("rtthread", "undetected")],
        "ambiguous": [
            ProbeCandidate("rtthread", "exact", version="4.0.5"),
            ProbeCandidate("rtthread", "exact", version="4.1.1"),
        ],
        "other-adapter": [ProbeCandidate("freertos", "exact", version="10.3.1")],
    }
    for name, candidates in cases.items():
        monkeypatch.setattr("gdr.probe.collect_candidates", lambda c=candidates: c)
        resolved = entrypoint._resolve_auto("rtt")
        expected = "4.0.5" if name == "exact" else None
        assert resolved == expected, name


def test_init_auto_resolves_or_reports_a_structured_failure(monkeypatch):
    """``gdr init <rtos> auto`` either initializes or explains itself."""
    entrypoint = _load_entrypoint()
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(entrypoint, "info", lambda _message: None)
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)
    monkeypatch.setattr(entrypoint, "_resolve_auto", lambda _rtos: "4.0.5")
    monkeypatch.setattr(
        entrypoint, "_setup_rtos", lambda rtos, version: calls.append((rtos, version))
    )

    entrypoint._init_command(["rtthread", "auto"])

    assert calls == [("rtthread", "4.0.5")]

    monkeypatch.setattr(entrypoint, "_resolve_auto", lambda _rtos: None)
    warnings: list[str] = []
    monkeypatch.setattr(entrypoint, "warn", warnings.append)
    entrypoint._init_command(["rtthread", "auto"])

    assert calls == [("rtthread", "4.0.5")]
    assert any("cannot resolve" in note for note in warnings)


def test_reset_command_clears_the_semantic_session(monkeypatch):
    """Reset unhooks printers, clears caches, then drops the session.

    Printer unregistration matters for a cross-RTOS re-init: the layout-derived
    lookup is registered once per session, so a stale one would fold new-RTOS
    objects through the previous adapter's layout.
    """
    entrypoint = _load_entrypoint()
    notes: list[str] = []
    resets: list[str] = []

    class _Adapter:
        def reset(self):
            resets.append("adapter")

    monkeypatch.setattr(entrypoint, "info", notes.append)
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: True)
    monkeypatch.setattr("gdr.adapter_api.active", lambda: _Adapter())
    monkeypatch.setattr("gdr.adapter_api.reset", lambda: resets.append("session"))
    monkeypatch.setattr(
        "gdr.printers.unregister_printers", lambda: resets.append("printers")
    )
    monkeypatch.setattr("gdr.gdb_bridge.reset_loadable_ranges", lambda: None)
    monkeypatch.setattr("gdr.api.next_generation", lambda: 1)

    entrypoint._reset_command()

    assert resets == ["adapter", "printers", "session"]
    assert notes and "reset" in notes[0]
