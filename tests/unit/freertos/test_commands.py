"""Unit tests for the complete FreeRTOS command route table."""

from __future__ import annotations

from pathlib import Path

import pytest

import freertos.commands as commands


class _FakeAdapter:
    """Minimal stand-in exposing a layout for completion tests."""

    def __init__(self) -> None:
        self.layout = object()


_PUBLIC_ROUTES = {
    "tasks": ("tasks", None),
    "system": ("system", None),
    "objects": ("objects", None),
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
        # Reason: the bare ``objects`` route owns its provenance summary; a
        # call with no kind would silently lose the Sources column.
        if not kind:
            raise AssertionError("'objects' must route to render_object_summary")
        calls.append(("objects", kind))

    monkeypatch.setattr(commands, "render_objects", render_objects)
    monkeypatch.setattr(
        commands,
        "render_object_summary",
        lambda: calls.append(("objects", None)),
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
    for alias, public_command in commands._COMMAND_ALIASES.items():
        assert alias in commands._HELP
        assert f"-> {public_command}" in commands._HELP
    # Help documents why the task table has no Entry column.
    assert "Entry" in commands._HELP
    assert "Single-object detail" in commands._HELP


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

    assert warnings == [commands._USAGE] * 2
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
    monkeypatch.setattr(commands, "active", lambda: _SummaryAdapter())
    monkeypatch.setattr(commands, "info", messages.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, elastic=(): captured.update(
            rows=rows, headers=headers, elastic=elastic
        ),
    )

    commands.render_object_summary()

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
