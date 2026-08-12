"""Unit tests for the complete RT-Thread command route table."""

from __future__ import annotations

import pytest

import rtthread.commands as commands

_PUBLIC_ROUTES = {
    "threads": ("tasks", None),
    "semaphores": ("objects", "semaphore"),
    "mutexes": ("objects", "mutex"),
    "events": ("objects", "event"),
    "mailboxs": ("objects", "mailbox"),
    "messagequeues": ("objects", "msgqueue"),
    "mempools": ("objects", "mempool"),
    "timers": ("objects", "timer"),
    "system": ("system", None),
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
        "render_objects",
        lambda kind: calls.append(("objects", kind)),
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
    for alias, public_command in commands._COMMAND_ALIASES.items():
        assert f"{alias}" in commands._HELP
        assert f"-> {public_command}" in commands._HELP


def test_unknown_or_extra_arguments_refer_to_help(monkeypatch):
    """Rejected input directs users to the complete command reference."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands._invoke_command("events extra")
    commands._invoke_command("help extra")
    commands._invoke_command("unknown")

    assert warnings == [commands._USAGE] * 3
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
    """Help lists every singular detail form with its canonical kind."""
    commands._invoke_command("help")

    help_output = capsys.readouterr().out
    for command in commands._SINGULAR_COMMANDS:
        assert f"rtt {command}" in help_output
    assert "Single-object detail" in help_output
