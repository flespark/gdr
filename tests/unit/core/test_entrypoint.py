"""Unit tests for the GDR entry point outside a GDB process."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import rtthread.version as version
from freertos.layout import FreeRtosConfig, FreeRtosLayout
from gdr.layout import KernelLayout
from gdr.version import TargetVerdict
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
    monkeypatch.setattr(
        version,
        "check_target",
        lambda value, **_kwargs: version_checks.append(value),
    )
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
    monkeypatch.setattr(
        version,
        "check_target",
        lambda _value, **_kwargs: TargetVerdict(version=(3, 1, 3)),
    )
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
    monkeypatch.setattr(
        version,
        "check_target",
        lambda _value, **_kwargs: TargetVerdict(version=(4, 0, 5)),
    )
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
    monkeypatch.setattr(
        "freertos.version.check_target",
        lambda _value, **_kwargs: TargetVerdict(version=(10, 3, 1)),
    )
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

    def _broken_setup(*_args, **_kwargs):
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

    def _record(rtos: str, version: str, **_kwargs):
        received.append([rtos, version])

    monkeypatch.setattr(entrypoint, "_setup_rtos", _record)
    entrypoint._invoke_command("init rtthread 4.0.5")
    assert received == [["rtthread", "4.0.5"]]


def test_init_allow_unverified_reaches_the_target_check(monkeypatch):
    """The escape hatch must travel from the flag into the adapter's verdict."""
    entrypoint = _load_entrypoint()
    seen: list[dict[str, object]] = []
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)
    monkeypatch.setattr(
        entrypoint,
        "_setup_rtos",
        lambda rtos, version, **kwargs: seen.append(
            {"rtos": rtos, "version": version, **kwargs}
        ),
    )

    entrypoint._invoke_command("init rtthread 4.0.5 --allow-unverified")

    assert seen == [{"rtos": "rtthread", "version": "4.0.5", "allow_unverified": True}]


def test_init_json_reports_an_unverified_registration(monkeypatch, capsys):
    """A client reads the verification state instead of parsing log lines."""
    entrypoint = _load_entrypoint()
    note = (
        "no RT-Thread symbol matched; registration was forced with --allow-unverified"
    )
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)
    monkeypatch.setattr(
        entrypoint,
        "_setup_rtos",
        lambda *_args, **_kwargs: TargetVerdict(version=(4, 0, 5), note=note),
    )

    entrypoint._invoke_command("init rtthread 4.0.5 --allow-unverified --json")

    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is True
    assert document["verified"] is False
    assert document["warning"] == note


def test_init_json_reports_a_refusal_with_a_code(monkeypatch, capsys):
    """A refused registration names the reason a client can act on."""
    entrypoint = _load_entrypoint()
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)
    monkeypatch.setattr(
        entrypoint,
        "_setup_rtos",
        lambda *_args, **_kwargs: TargetVerdict(
            code="no_target_evidence", message="no RT-Thread symbol matched"
        ),
    )

    entrypoint._invoke_command("init rtthread 4.0.5 --json")

    document = json.loads(capsys.readouterr().out)
    assert document["ok"] is False
    assert document["error"] == {
        "code": "no_target_evidence",
        "message": "no RT-Thread symbol matched",
    }


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


def test_a_documented_topic_is_reported_as_a_topic_not_a_command(monkeypatch, capsys):
    """A ``gdr help`` topic run as a subcommand names the way to open it."""
    entrypoint = _load_entrypoint()
    warnings: list[str] = []
    monkeypatch.setattr(entrypoint, "warn", warnings.append)

    entrypoint._invoke_command("pretty-printers timer")

    assert warnings == [
        "'pretty-printers' is a help topic, not a command; "
        "run 'gdr help pretty-printers'"
    ]
    assert capsys.readouterr().out == ""


def test_unknown_command_points_at_the_rtos_tree_when_an_adapter_is_active(
    monkeypatch, capsys
):
    """``gdr heap`` must not read as "not initialized" in an initialized session.

    Reason: a kernel object spelled as a ``gdr`` subcommand used to print the
    ``gdr init`` usage, which sent the user looking for an initialization problem
    that did not exist; kernel state belongs to the RTOS command tree.
    """
    entrypoint = _load_entrypoint()
    warnings: list[str] = []
    monkeypatch.setattr(entrypoint, "warn", warnings.append)
    monkeypatch.setattr("gdr.adapter_api.session_meta", lambda: ("freertos", "11.1.0"))

    entrypoint._invoke_command("heap -h")

    assert len(warnings) == 1
    assert "unknown command 'heap'" in warnings[0]
    assert "'freertos heap'" in warnings[0]
    assert "gdr init" not in warnings[0]
    assert capsys.readouterr().out == ""


def test_unknown_command_before_init_says_to_initialize_first(monkeypatch, capsys):
    """With no adapter registered the message says how to get one."""
    entrypoint = _load_entrypoint()
    warnings: list[str] = []
    monkeypatch.setattr(entrypoint, "warn", warnings.append)
    monkeypatch.setattr("gdr.adapter_api.session_meta", lambda: (None, None))

    entrypoint._invoke_command("heap")

    assert len(warnings) == 1
    assert "unknown command 'heap'" in warnings[0]
    assert "gdr init <rtos> <version>" in warnings[0]
    assert "<rtos> heap" in warnings[0]
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
    entrypoint = _load_entrypoint()
    monkeypatch.setattr(
        entrypoint,
        "_setup_rtos",
        lambda *_args, **_kwargs: TargetVerdict(
            code="no_target_evidence", message="no RT-Thread symbol matched"
        ),
    )
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: False)

    entrypoint._init_command(["rtthread", "4.0.5", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result["rtos"] == "rtthread"
    assert result["error"]["code"] == "no_target_evidence"


def test_init_force_resets_before_replacing(monkeypatch):
    """``--force`` drops the active adapter instead of refusing."""
    entrypoint = _load_entrypoint()
    calls: list[str] = []
    monkeypatch.setattr(entrypoint, "_reset_command", lambda: calls.append("reset"))
    monkeypatch.setattr(
        entrypoint,
        "_setup_rtos",
        lambda _rtos, _version, **_kwargs: calls.append("setup"),
    )
    monkeypatch.setattr("gdr.adapter_api.is_initialized", lambda: True)

    entrypoint._init_command(["freertos", "10.3.1", "--force"])

    assert calls == ["reset", "setup"]


def test_format_rtthread_config_lists_valued_options_and_toggles():
    """The init banner names valued configs plus enable/disable toggles."""
    entrypoint = _load_entrypoint()
    cfg = RtConfig(
        smp=True,
        cpu_count=2,
        heap_type="small_mem",
        using_semaphore=True,
        using_mutex=True,
        using_event=False,
        using_mailbox=True,
        using_messagequeue=True,
        using_mempool=False,
        using_soft_timer=True,
        using_memtrace=False,
    )

    assert entrypoint._format_rtthread_config(cfg) == (
        "config: smp: enable, cores: 2, heap: small_mem, semaphore: enable, "
        "mutex: enable, event: disable, mailbox: enable, messagequeue: enable, "
        "mempool: disable, timer-soft: enable, memtrace: disable"
    )


def test_format_rtthread_config_omits_cores_on_single_core():
    """Core counts ride the SMP entry and stay out of UP banners."""
    entrypoint = _load_entrypoint()

    assert "cores" not in entrypoint._format_rtthread_config(RtConfig())
    assert "smp" not in entrypoint._format_rtthread_config(RtConfig())


def test_format_freertos_config_aligns_with_rtthread():
    """Same k/v shape; tick width and notification counts stay out."""
    entrypoint = _load_entrypoint()
    cfg = FreeRtosConfig(
        smp=True,
        number_of_cores=2,
        tick_bits=16,
        notification_count=8,
        heap_kind=4,
        heap_protector=True,
        timers=True,
        event_groups=True,
        stream_buffers=False,
        queue_support=True,
        queue_registry=True,
        queue_registry_size=9,
        queue_sets=False,
        trace_facility=True,
        mpu_object_pool=False,
    )

    assert entrypoint._format_freertos_config(cfg) == (
        "config: smp: enable, cores: 2, heap: heap_4+protector, timers: enable, "
        "event-groups: enable, stream-buffers: disable, queues: enable, "
        "queue-sets: disable, trace: enable, mpu: disable, "
        "queue-registry: enable (size 9)"
    )


def test_format_freertos_config_omits_smp_and_names_unknown_heap():
    """Single-core builds skip placement; a symbol-less heap is unknown."""
    entrypoint = _load_entrypoint()
    banner = entrypoint._format_freertos_config(FreeRtosConfig())

    assert "smp" not in banner
    assert "cores" not in banner
    assert "tick" not in banner
    assert "heap: unknown" in banner


def test_stop_hook_and_reset_cover_the_watch_set():
    """Watch printing rides the guarded stop hook; reset drops the set.

    The event loop only exists inside GDB, so the wiring is asserted on the
    module source -- the only artifact that exists in both worlds.
    """
    entrypoint_file = _load_entrypoint().__file__
    assert entrypoint_file is not None
    source = Path(entrypoint_file).read_text()
    assert "render_watches()" in source
    assert "clear_watches()" in source


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
        entrypoint,
        "_setup_rtos",
        lambda rtos, version, **_kwargs: calls.append((rtos, version)),
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
