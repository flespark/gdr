"""Unit tests for the complete RT-Thread command route table."""

from __future__ import annotations

import pytest

import rtthread.commands as commands
from gdr import commands as core_commands


class _FakeAdapter:
    """Minimal stand-in exposing a layout for completion tests."""

    def __init__(self) -> None:
        self.layout = object()


_PUBLIC_ROUTES = {
    "threads": ("tasks", None),
    "semaphores": ("objects", "semaphore"),
    "mutexes": ("objects", "mutex"),
    "events": ("objects", "event"),
    "mailboxs": ("objects", "mailbox"),
    "messagequeues": ("objects", "msgqueue"),
    "mempools": ("objects", "mempool"),
    "timers": ("objects", "timer"),
    "objects": ("objects", ""),
    "system": ("system", None),
    "heap": ("heap", None),
    "check": ("check", None),
}


@pytest.mark.parametrize(("command", "expected"), _PUBLIC_ROUTES.items())
def test_public_commands_reach_their_semantic_renderer(command, expected, monkeypatch):
    """Every documented command reaches exactly one normalized renderer."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(commands, "render_tasks", lambda: calls.append(("tasks", None)))
    monkeypatch.setattr(
        commands, "render_system", lambda: calls.append(("system", None))
    )
    monkeypatch.setattr(
        commands,
        "render_heap",
        lambda: calls.append(("heap", None)),
    )
    monkeypatch.setattr(
        commands,
        "render_objects",
        lambda kind: calls.append(("objects", kind)),
    )
    monkeypatch.setattr(
        commands, "render_check", lambda _args: calls.append(("check", None))
    )

    commands._invoke_command(command)

    assert calls == [expected]


@pytest.mark.parametrize(
    ("alias", "public_command"),
    (
        ("tasks", "threads"),
        ("sems", "semaphores"),
        ("mtxs", "mutexes"),
        ("msgs", "messagequeues"),
        ("mboxs", "mailboxs"),
        ("mailboxes", "mailboxs"),
    ),
)
def test_command_aliases_share_the_public_route(alias, public_command, monkeypatch):
    """Aliases dispatch through the same semantic route as their long form."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(commands, "render_tasks", lambda: calls.append(("tasks", None)))
    monkeypatch.setattr(
        commands,
        "render_objects",
        lambda kind: calls.append(("objects", kind)),
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
        assert f"rtt {command}" in commands._HELP
    for command in commands._DETAIL_DESCRIPTIONS:
        assert f"rtt {command}" in commands._HELP
    for alias, public_command in commands._COMMAND_ALIASES.items():
        assert alias in commands._HELP
        assert f"-> rtt {public_command}" in commands._HELP
    assert "Run 'rtt help <topic>'" in commands._HELP


def test_help_topic_documents_fields_tips_config_and_limits(capsys):
    """List, detail, heap and printer nodes expose structured sections."""
    commands._invoke_command("help threads")
    threads = capsys.readouterr().out
    commands._invoke_command("help thread")
    thread = capsys.readouterr().out
    commands._invoke_command("help heap")
    heap = capsys.readouterr().out

    for output in (threads, thread, heap):
        assert "DESCRIPTION" in output
        assert "FIELDS" in output
        assert "TIPS" in output
        assert "CONFIGURATION" in output
        assert "LIMITATIONS" in output
    assert "Holes" not in heap and "Thread occupancy" not in heap
    assert "dump" in heap


def test_unknown_or_extra_arguments_refer_to_help(monkeypatch):
    """Rejected input directs users to the complete command reference."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands._invoke_command("events extra")
    commands._invoke_command("help extra")
    commands._invoke_command("unknown")
    commands._invoke_command("pretty-printers timer")

    assert warnings == [commands._USAGE] * 4
    assert "rtt help" in commands._USAGE


@pytest.mark.parametrize(
    ("command", "name", "expected_kind"),
    (
        ("thread", "worker1", "task"),
        ("timer", "test_timer", "timer"),
        ("semaphore", "test_sem", "semaphore"),
        ("mutex", "test_mutex", "mutex"),
        ("event", "test_event", "event"),
        ("mailbox", "test_mailbox", "mailbox"),
        ("messagequeue", "test_msgqueue", "msgqueue"),
        ("mempool", "test_mempool", "mempool"),
    ),
)
def test_singular_commands_route_to_object_detail(
    command, name, expected_kind, monkeypatch
):
    """``rtt <object> <name>`` dispatches to the detail renderer."""
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

    commands._invoke_command("thread")

    assert calls == []
    assert warnings == [commands._USAGE]


def test_help_documents_singular_detail_syntax(capsys):
    """Help lists every singular detail form and points to topic lookup."""
    commands._invoke_command("help")

    help_output = capsys.readouterr().out
    for command in commands._SINGULAR_COMMANDS:
        assert f"rtt {command}" in help_output
    assert "Run 'rtt help <topic>'" in help_output


def test_complete_suggests_commands_for_the_first_word():
    """The first argument completes against the command vocabulary."""
    candidates = commands._complete("sem", "sem")

    assert "semaphores" in candidates
    assert "semaphore" in candidates
    assert all(candidate.startswith("sem") for candidate in candidates)


def test_complete_commands_filters_by_prefix():
    """Command completion honours the partial prefix."""
    candidates = commands._complete("ev", "ev")

    assert "events" in candidates
    assert "event" in candidates
    assert all(candidate.startswith("ev") for candidate in candidates)


def test_complete_object_names_walks_live_kernel(monkeypatch):
    """The second argument of a detail command completes real object names."""
    adapter = _FakeAdapter()
    monkeypatch.setattr(commands, "RtThreadAdapter", _FakeAdapter)
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "iter_object_names",
        lambda _kind, _layout: ["worker1", "worker2", "main"],
    )

    candidates = commands._complete("thread wor", "wor")

    assert candidates == ["worker1", "worker2"]


def test_complete_object_names_uses_the_command_semantic_kind(monkeypatch):
    """The detail command selects the matching kernel object registry."""
    adapter = _FakeAdapter()
    monkeypatch.setattr(commands, "RtThreadAdapter", _FakeAdapter)
    seen_kinds: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "iter_object_names",
        lambda kind, _layout: seen_kinds.append(kind) or [],
    )

    commands._complete("messagequeue test", "test")

    assert seen_kinds == ["msgqueue"]


def test_complete_object_names_degrades_without_rtthread_adapter(monkeypatch):
    """No active RT-Thread adapter yields no object-name candidates."""
    monkeypatch.setattr(commands, "active", lambda: object())
    monkeypatch.setattr(
        commands,
        "iter_object_names",
        lambda _kind, _layout: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert commands._complete("semaphore tes", "tes") == []


def test_complete_object_names_degrades_on_traversal_failure(monkeypatch):
    """A broken registry walk never raises inside GDB completion."""
    adapter = _FakeAdapter()
    monkeypatch.setattr(commands, "active", lambda: adapter)

    def broken(_kind, _layout):
        raise RuntimeError("target unreachable")

    monkeypatch.setattr(commands, "iter_object_names", broken)

    assert commands._complete("semaphore tes", "tes") == []


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

    assert commands._invoke_command("threads") is None
    assert errors == ["_invoke_command: ValueError: corrupt task list"]


def test_complete_plural_list_command_does_not_walk_objects(monkeypatch):
    """Plural list commands never trigger object-name traversal."""
    monkeypatch.setattr(
        commands,
        "active",
        lambda: (_ for _ in ()).throw(AssertionError("unused")),
    )
    monkeypatch.setattr(
        commands,
        "iter_object_names",
        lambda _kind, _layout: (_ for _ in ()).throw(AssertionError("unused")),
    )

    candidates = commands._complete("sema", "sema")

    assert "semaphores" in candidates
    assert "semaphore" in candidates


class _FakeHeapAdapter(commands.RtThreadAdapter):
    """RtThreadAdapter stand-in that hands back a fixed heap report.

    The report is what the real adapter would return; these tests pin the
    command's output shape, while test_adapter pins how the report is built.
    """

    def __init__(self, report):
        self._report = report

    def heap_report(self):
        return self._report


def test_render_heap_prints_basics_and_verdict_free_of_removed_rows(monkeypatch):
    """``rtt heap`` prints the basics and Blocks line; no Holes or occupancy rows."""
    from gdr.adapter_api import HeapReport

    adapter = _FakeHeapAdapter(
        HeapReport(
            pairs=[
                ("Algorithm", "small_mem"),
                ("Blocks", "12 used, 5 free, 17 total"),
            ],
        )
    )
    printed: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(core_commands, "active", lambda: adapter)
    monkeypatch.setattr(core_commands, "print_detail", printed.append)
    monkeypatch.setattr(
        core_commands,
        "print_table",
        lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("no table")),
    )

    commands.render_heap()

    assert printed == [
        [("Algorithm", "small_mem"), ("Blocks", "12 used, 5 free, 17 total")]
    ]


def test_render_heap_degrades_to_na_without_a_walk(monkeypatch):
    """An unresolvable block chain keeps the basics and marks the Blocks line N/A."""
    from gdr.adapter_api import HeapReport

    adapter = _FakeHeapAdapter(
        HeapReport(pairs=[("Algorithm", "small_mem"), ("Blocks", "N/A")])
    )
    printed: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(core_commands, "active", lambda: adapter)
    monkeypatch.setattr(core_commands, "print_detail", printed.append)

    commands.render_heap()

    assert dict(printed[0])["Blocks"] == "N/A"
    assert "Holes" not in dict(printed[0])


def test_render_heap_degrades_an_adapter_without_heap_report(monkeypatch):
    """The neutral render_heap accepts any adapter implementing heap_report.

    ``rtt heap`` shared the core renderer with ``frt heap`` (C1), so the old
    per-RTOS isinstance guard is gone: an active adapter missing the protocol
    method degrades through the command guard instead of a custom warning.
    """
    from gdr import gdb_bridge as bridge

    errors: list[str] = []
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)
    monkeypatch.setattr(commands, "active", lambda: object())

    commands.render_heap()

    assert errors and "render_heap" in errors[0]
