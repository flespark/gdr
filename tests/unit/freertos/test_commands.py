"""Unit tests for the complete FreeRTOS command route table."""

from __future__ import annotations

from pathlib import Path

import pytest

import freertos.adapter as adapter_module
import freertos.commands as commands
from freertos.layout import FreeRtosConfig, build_layout
from freertos.navigation import DiscoveredObject


class _FakeAdapter:
    """Minimal stand-in exposing a layout for completion tests."""

    def __init__(self) -> None:
        self.layout = object()


_PUBLIC_ROUTES = {
    "tasks": ("tasks", None),
    "system": ("system", None),
    "objects": ("objects", ""),
    "heap": ("heap", None),
    "queues": ("objects", "queue"),
    "semaphores": ("objects", "semaphore"),
    "mutexes": ("objects", "mutex"),
    "timers": ("objects", "timer"),
    "eventgroups": ("objects", "eventgroup"),
    "streambuffers": ("objects", "streambuffer"),
}


@pytest.mark.parametrize(("command", "expected"), _PUBLIC_ROUTES.items())
def test_public_commands_reach_their_semantic_renderer(command, expected, monkeypatch):
    """Every documented command reaches exactly one normalized renderer."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(commands, "render_tasks", lambda: calls.append(("tasks", None)))
    monkeypatch.setattr(
        commands, "render_system", lambda: calls.append(("system", None))
    )

    def render_objects(kind: str = ""):
        # The bare ``objects`` route reaches the neutral renderer with no
        # kind; that renderer asks the adapter for its provenance summary.
        calls.append(("objects", kind))

    monkeypatch.setattr(commands, "render_objects", render_objects)
    monkeypatch.setattr(
        commands, "render_system", lambda: calls.append(("system", None))
    )
    monkeypatch.setattr(commands, "render_heap", lambda: calls.append(("heap", None)))

    commands._invoke_command(command)

    assert calls == [expected]


@pytest.mark.parametrize(
    ("alias", "public_command"),
    (
        ("threads", "tasks"),
        ("sems", "semaphores"),
        ("mtxs", "mutexes"),
        ("qs", "queues"),
        ("egs", "eventgroups"),
        ("sbs", "streambuffers"),
    ),
)
def test_command_aliases_share_the_public_route(alias, public_command, monkeypatch):
    """Aliases dispatch through the same semantic route as their long form."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(commands, "render_tasks", lambda: calls.append(("tasks", None)))
    monkeypatch.setattr(
        commands,
        "render_objects",
        lambda kind="": calls.append(("objects", kind or None)),
    )

    commands._invoke_command(alias)

    assert calls == [_PUBLIC_ROUTES[public_command]]


@pytest.mark.parametrize("argument", ("", "help", "HELP"))
def test_help_lists_every_command_and_alias(argument, capsys):
    """Help is available explicitly and for an empty command line."""
    commands._invoke_command(argument)

    assert capsys.readouterr().out == f"{commands._HELP}\n"
    assert set(commands._COMMAND_DESCRIPTIONS) == {"help", *_PUBLIC_ROUTES}
    for command in commands._COMMAND_DESCRIPTIONS:
        assert f"frt {command}" in commands._HELP
    for command in commands._DETAIL_DESCRIPTIONS:
        assert f"frt {command}" in commands._HELP
    for alias, public_command in commands._COMMAND_ALIASES.items():
        assert alias in commands._HELP
        assert f"-> frt {public_command}" in commands._HELP
    assert "Run 'frt help <topic>'" in commands._HELP


def test_help_topic_documents_fields_tips_config_and_limits(capsys):
    """Every command is a detailed topic, including list and detail forms."""
    commands._invoke_command("help tasks")
    tasks = capsys.readouterr().out
    commands._invoke_command("help task")
    task = capsys.readouterr().out
    commands._invoke_command("help heap")
    heap = capsys.readouterr().out

    for output in (tasks, task, heap):
        assert "DESCRIPTION" in output
        assert "FIELDS" in output
        assert "TIPS" in output
        assert "CONFIGURATION" in output
        assert "LIMITATIONS" in output
    assert "Entry column" in tasks
    assert "owner field" in heap


def test_help_extra_args_rejected(monkeypatch):
    """``help extra`` is not a valid detail command and refers to usage."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands._invoke_command("help extra")

    assert warnings == [commands._USAGE]


def test_unknown_or_extra_arguments_refer_to_help(monkeypatch):
    """Rejected input directs users to the complete command reference."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands._invoke_command("tasks extra")
    commands._invoke_command("unknown")
    commands._invoke_command("pretty-printers timer")

    assert warnings == [commands._USAGE] * 3
    assert "frt help" in commands._USAGE


@pytest.mark.parametrize(
    ("command", "name", "expected_kind"),
    (
        ("task", "worker1", "task"),
        ("queue", "serial", "queue"),
        ("semaphore", "bin", "semaphore"),
        ("mutex", "fw_mutex", "mutex"),
        ("timer", "watchdog", "timer"),
        ("eventgroup", "events", "eventgroup"),
        ("streambuffer", "stream", "streambuffer"),
    ),
)
def test_singular_commands_route_to_object_detail(
    command, name, expected_kind, monkeypatch
):
    """``frt <object> <name>`` dispatches to the detail renderer."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        commands,
        "render_object_detail",
        lambda kind, obj_name: calls.append((kind, obj_name)),
    )

    commands._invoke_command(f"{command} {name}")

    assert calls == [(expected_kind, name)]


def test_singular_command_without_name_refers_to_help(monkeypatch):
    """A bare singular object name without a detail argument is rejected."""
    warnings: list[str] = []
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(commands, "render_object_detail", calls.append)

    commands._invoke_command("task")

    assert calls == []
    assert warnings == [commands._USAGE]


def test_complete_suggests_commands_for_the_first_word():
    """The first argument completes against the command vocabulary."""
    candidates = commands._complete("ta", "ta")

    assert "tasks" in candidates
    assert "task" in candidates
    assert all(candidate.startswith("ta") for candidate in candidates)


def test_complete_commands_filters_by_prefix():
    """Command completion honours the partial prefix."""
    candidates = commands._complete("se", "se")

    assert "semaphores" in candidates
    assert "semaphore" in candidates
    assert all(candidate.startswith("se") for candidate in candidates)


def test_complete_singular_second_arg_walks_objects(monkeypatch):
    """The second argument of a detail command completes live task names."""
    adapter = _FakeAdapter()
    monkeypatch.setattr(commands, "FreeRtosAdapter", _FakeAdapter)
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "iter_task_names",
        lambda _layout: ["worker1", "worker2", "main"],
    )

    candidates = commands._complete("task wor", "wor")

    assert candidates == ["worker1", "worker2"]


def test_complete_object_names_degrade_without_freertos_adapter(monkeypatch):
    """No active FreeRTOS adapter yields no object-name candidates."""
    monkeypatch.setattr(commands, "active", lambda: object())
    monkeypatch.setattr(
        commands,
        "iter_task_names",
        lambda _layout: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert commands._complete("task tes", "tes") == []


def test_complete_object_names_degrade_on_traversal_failure(monkeypatch):
    """A broken traversal never raises inside GDB completion."""
    adapter = _FakeAdapter()
    monkeypatch.setattr(commands, "FreeRtosAdapter", _FakeAdapter)
    monkeypatch.setattr(commands, "active", lambda: adapter)

    def broken(_layout):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr(commands, "iter_task_names", broken)

    assert commands._complete("task tes", "tes") == []


def test_complete_plural_list_command_does_not_walk_objects(monkeypatch):
    """Plural list commands never trigger object-name traversal."""
    monkeypatch.setattr(
        commands,
        "active",
        lambda: (_ for _ in ()).throw(AssertionError("unused")),
    )
    monkeypatch.setattr(
        commands,
        "iter_task_names",
        lambda _layout: (_ for _ in ()).throw(AssertionError("unused")),
    )

    candidates = commands._complete("sema", "sema")

    assert "semaphores" in candidates
    assert "semaphore" in candidates


def test_command_entry_guard_contains_unexpected_renderer_errors(monkeypatch):
    """A broken renderer cannot leak a Python exception out of the command edge."""
    from gdr import gdb_bridge as bridge

    errors: list[str] = []
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)
    monkeypatch.setattr(
        commands,
        "render_tasks",
        lambda: (_ for _ in ()).throw(ValueError("corrupt task list")),
    )

    assert commands._invoke_command("tasks") is None
    assert errors == ["_invoke_command: ValueError: corrupt task list"]


def test_command_class_does_not_pass_a_completer_class():
    """FreeRtosCommand must not pass a completer_class to its base.

    Passing any completer_class (e.g. ``gdb.COMPLETE_COMMAND``) makes GDB skip
    the Python ``complete()`` method entirely, so subcommand and task-name
    completion would silently never run. Unit tests import the module outside
    GDB, where the class does not even exist, so the contract is asserted on
    the module source -- the only artifact that exists in both worlds.
    """
    code_lines = [
        line
        for line in Path(commands.__file__).read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    source = "\n".join(code_lines)

    assert "gdb.COMPLETE_" not in source, "a completer_class disables complete()"
    assert 'super().__init__("freertos", gdb.COMMAND_USER)' in source
    assert "def complete(" in source


def test_singular_command_accepts_a_name_containing_spaces(monkeypatch):
    """FreeRTOS ships object names with spaces; they must stay reachable.

    The timer daemon task is named "Tmr Svc" (configTIMER_SERVICE_TASK_NAME),
    so treating the name as a single token made ``frt task Tmr Svc`` fall into
    the usage warning and left that task's detail unreachable.
    """
    calls: list[tuple[str, str]] = []
    warnings: list[str] = []
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "render_object_detail",
        lambda kind, obj_name: calls.append((kind, obj_name)),
    )

    commands._invoke_command("task Tmr Svc")

    assert calls == [("task", "Tmr Svc")]
    assert warnings == []


def test_plural_command_with_extra_words_still_rejected(monkeypatch):
    """Only singular detail commands consume trailing words as a name."""
    warnings: list[str] = []
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(commands, "render_object_detail", calls.append)

    commands._invoke_command("tasks Tmr Svc")

    assert calls == []
    assert warnings == [commands._USAGE]


def test_objects_summary_headers_and_sources(monkeypatch):
    """``frt objects`` renders Kind/Count/Sources with a channel breakdown.

    The core renderer only knows Kind/Count; the FreeRTOS summary owns the
    provenance model, so the table must carry the per-channel source counts.
    """
    from gdr import commands as gdr_commands
    from gdr.adapter_api import ObjectTable

    class _SummaryAdapter:
        def object_summary_table(self) -> ObjectTable:
            return ObjectTable(
                headers=["Kind", "Count", "Sources"],
                rows=[
                    ["task", "16", "scheduler=16"],
                    ["queue", "3", "symbol=3 registry=1"],
                ],
                messages=["limitation note"],
                elastic=("Sources",),
            )

    messages: list[str] = []
    captured: dict[str, object] = {"messages": messages}
    monkeypatch.setattr(commands, "FreeRtosAdapter", _SummaryAdapter)
    # The neutral renderer reads the active adapter itself.
    monkeypatch.setattr(gdr_commands, "active", lambda: _SummaryAdapter())
    # The provenance summary renders through the neutral renderer (C1), so
    # info/print_table live in gdr.commands.
    monkeypatch.setattr(gdr_commands, "info", messages.append)
    monkeypatch.setattr(
        gdr_commands,
        "print_table",
        lambda rows, headers, elastic=(): captured.update(
            rows=rows, headers=headers, elastic=elastic
        ),
    )

    gdr_commands.render_objects("")

    assert captured["headers"] == ["Kind", "Count", "Sources"]
    assert captured["rows"] == [
        ["task", "16", "scheduler=16"],
        ["queue", "3", "symbol=3 registry=1"],
    ]
    assert captured["messages"] == ["limitation note"]
    assert captured["elastic"] == ("Sources",)


def test_complete_second_arg_walks_discovered_object_names(monkeypatch):
    """Non-task detail commands complete against discovery-channel names."""
    from freertos.navigation import DiscoveredObject

    adapter = _FakeAdapter()
    monkeypatch.setattr(commands, "FreeRtosAdapter", _FakeAdapter)
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "discover",
        lambda _kind, _layout: [
            DiscoveredObject(
                kind="queue", address=0x1, name="gdr_queue", source="registry"
            ),
            DiscoveredObject(
                kind="queue", address=0x2, name="gdr_other", source="symbol"
            ),
        ],
    )

    candidates = commands._complete("queue gdr_", "gdr_")

    assert candidates == ["gdr_other", "gdr_queue"]


# ---------------------------------------------------------------------------
# queue-family list tables
# ---------------------------------------------------------------------------


def _queue_adapter(monkeypatch, config, found, objects):
    """Build an adapter whose object_table renders *objects* row cells."""
    adapter = adapter_module.FreeRtosAdapter(build_layout(config, (10, 3, 1)))
    monkeypatch.setattr(adapter_module, "discover", lambda _kind, _layout: found)
    monkeypatch.setattr(adapter_module, "_cast_object", lambda _a, _k, _l: object())
    monkeypatch.setattr(
        adapter_module,
        "value_to_queue_object",
        lambda _value, found_obj, _layout: objects[found.index(found_obj)],
    )
    return adapter


def test_queues_table_headers_and_locks_column(monkeypatch):
    """frt queues owns its verbatim header contract and the Locks cell."""
    found = [
        DiscoveredObject(
            kind="queue", address=0x2000, name="gdr_queue", source="registry"
        ),
        DiscoveredObject(
            kind="queue", address=0x3000, name="gdr_locked", source="symbol"
        ),
    ]
    unlocked = adapter_module.FreeRtosQueueObject(
        name="gdr_queue",
        address=0x2000,
        kind="queue",
        type_code=0,
        source="registry",
        length=4,
        item_size=4,
        count=0,
        free=4,
        send_waiters=[],
        recv_waiters=["gdr_qrecv"],
        rx_lock=-1,
        tx_lock=-1,
    )
    locked = adapter_module.FreeRtosQueueObject(
        name="gdr_locked",
        address=0x3000,
        kind="queue",
        type_code=0,
        source="symbol",
        length=2,
        item_size=4,
        count=1,
        free=1,
        send_waiters=None,
        recv_waiters=None,
        rx_lock=2,
        tx_lock=5,
        set_container=0x4000,
    )
    with_sets = _queue_adapter(
        monkeypatch,
        FreeRtosConfig(queue_sets=True, trace_facility=True),
        found,
        [unlocked, locked],
    )

    table = with_sets.object_table("queue")

    assert table.headers == [
        "Name",
        "Type",
        "Items",
        "Length",
        "ItemSize",
        "Free",
        "SendWait",
        "RecvWait",
        "Locks",
        "Set",
        "Src",
        "Addr",
    ]
    assert table.elastic == ("SendWait", "RecvWait", "Name")
    rows = {row[0]: row for row in table.rows}
    assert rows["gdr_queue"][8] == "-"
    assert rows["gdr_locked"][8] == "rx=2 tx=5"
    assert rows["gdr_queue"][9] == "-"  # not a set member
    assert rows["gdr_locked"][9] == "0x4000"
    assert rows["gdr_queue"][6] == "0"
    assert rows["gdr_queue"][7] == "1@gdr_qrecv"

    # Without queue sets the Set column is dropped entirely, not N/A-filled.
    without_sets = _queue_adapter(
        monkeypatch,
        FreeRtosConfig(trace_facility=True),
        found,
        [unlocked, locked],
    )
    no_set_table = without_sets.object_table("queue")
    assert no_set_table.headers == [
        "Name",
        "Type",
        "Items",
        "Length",
        "ItemSize",
        "Free",
        "SendWait",
        "RecvWait",
        "Locks",
        "Src",
        "Addr",
    ]


def test_semaphores_and_mutexes_table_headers(monkeypatch):
    """frt semaphores/mutexes own their verbatim header contracts."""
    sem_found = [
        DiscoveredObject(
            kind="semaphore", address=0x5000, name="gdr_semaphore", source="registry"
        )
    ]
    sem_obj = adapter_module.FreeRtosQueueObject(
        name="gdr_semaphore",
        address=0x5000,
        kind="semaphore",
        type_code=2,
        source="registry",
        length=3,
        count=0,
        recv_waiters=["gdr_semw"],
    )
    sem_table = _queue_adapter(
        monkeypatch, FreeRtosConfig(trace_facility=True), sem_found, [sem_obj]
    ).object_table("semaphore")
    assert sem_table.headers == [
        "Name",
        "Type",
        "Count",
        "Max",
        "Waiters",
        "Src",
        "Addr",
    ]
    assert sem_table.rows[0] == [
        "gdr_semaphore",
        "counting-sem",
        "0",
        "3",
        "1@gdr_semw",
        "registry",
        "0x5000",
    ]

    mtx_found = [
        DiscoveredObject(
            kind="mutex", address=0x6000, name="gdr_mutex", source="registry"
        )
    ]
    mtx_obj = adapter_module.FreeRtosQueueObject(
        name="gdr_mutex",
        address=0x6000,
        kind="mutex",
        type_code=1,
        source="registry",
        count=0,
        holder_address=0x7000,
        holder="main",
        recursive_count=0,
        recv_waiters=["gdr_mtxw"],
    )
    mtx_table = _queue_adapter(
        monkeypatch, FreeRtosConfig(trace_facility=True), mtx_found, [mtx_obj]
    ).object_table("mutex")
    assert mtx_table.headers == [
        "Name",
        "Type",
        "Held",
        "Owner",
        "Recursive",
        "Waiters",
        "Src",
        "Addr",
    ]
    assert mtx_table.rows[0] == [
        "gdr_mutex",
        "mutex",
        "yes",
        "main",
        "0",
        "1@gdr_mtxw",
        "registry",
        "0x6000",
    ]

    # A build without software timers still answers the command with an
    # explicit capability note instead of "not enumerable".
    timer_table = adapter_module.FreeRtosAdapter(
        build_layout(FreeRtosConfig(), (10, 3, 1))
    ).object_table("timer")
    assert timer_table is not None
    assert timer_table.rows == []
    assert any("no software timers" in message for message in timer_table.messages)
    # Event groups and stream buffers behave the same way: an absent
    # subsystem is a capability note over an empty table, never a silent
    # zero-count or a "not enumerable" None (discovery is stubbed empty so
    # the rows cannot leak other kinds in).
    monkeypatch.setattr(adapter_module, "discover", lambda _kind, _layout: [])
    eg_table = adapter_module.FreeRtosAdapter(
        build_layout(FreeRtosConfig(), (10, 3, 1))
    ).object_table("eventgroup")
    assert eg_table is not None
    assert eg_table.rows == []
    assert any("no event groups" in message for message in eg_table.messages)
    sb_table = adapter_module.FreeRtosAdapter(
        build_layout(FreeRtosConfig(), (10, 3, 1))
    ).object_table("streambuffer")
    assert sb_table is not None
    assert sb_table.rows == []
    assert any("no stream buffers" in message for message in sb_table.messages)
