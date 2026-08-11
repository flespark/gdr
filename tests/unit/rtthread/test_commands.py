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

    commands._invoke_command(command, "help")

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

    commands._invoke_command(alias, "help")

    assert calls == [_PUBLIC_ROUTES[public_command]]


def test_unknown_or_extra_arguments_report_the_complete_usage(monkeypatch):
    """Rejected input advertises every supported RT-Thread command."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands._invoke_command("events extra", "help")
    commands._invoke_command("unknown", "help")

    assert warnings == [commands._USAGE, commands._USAGE]
    for command in _PUBLIC_ROUTES:
        assert command in commands._USAGE
