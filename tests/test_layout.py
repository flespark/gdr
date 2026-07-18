"""Unit tests for generic intrusive-list traversal."""

from __future__ import annotations

import gdr.layout as layout


class _FakeGdb:
    """GDB exceptions used by ``iter_list``."""

    class error(Exception):
        pass

    class MemoryError(Exception):
        pass


class _FakeNode:
    """Minimal addressable list node."""

    def __init__(self, address: int):
        self.address = address
        self.next: _FakeNode | None = None

    def __int__(self) -> int:
        return self.address


_HOOK = layout.ListHook(
    head_expr="head",
    node_path=("node",),
    container_type="struct item",
    next_path=("next",),
)


def _configure_iter_list(monkeypatch) -> list[str]:
    """Replace GDB accessors with linked fake nodes."""
    warnings: list[str] = []
    monkeypatch.setattr(layout, "gdb", _FakeGdb)
    monkeypatch.setattr(layout, "read_path", lambda value, _path: value.next)
    monkeypatch.setattr(
        layout,
        "container_of",
        lambda node, _container_type, _member_path: node,
    )
    monkeypatch.setattr(layout, "warn", warnings.append)
    return warnings


def test_iter_list_terminates_at_the_list_head(monkeypatch):
    """A well-formed sentinel list completes without a corruption warning."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    first = _FakeNode(0x200)
    second = _FakeNode(0x300)
    head.next = first
    first.next = second
    second.next = head

    assert list(layout.iter_list(head, _HOOK)) == [first, second]
    assert warnings == []


def test_iter_list_warns_and_stops_at_a_corrupted_cycle(monkeypatch):
    """A cycle that excludes the list head must not yield duplicate nodes."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    first = _FakeNode(0x200)
    second = _FakeNode(0x300)
    head.next = first
    first.next = second
    second.next = first

    assert list(layout.iter_list(head, _HOOK)) == [first, second]
    assert len(warnings) == 1
    assert "repeated node" in warnings[0]
    assert "corrupted cycle" in warnings[0]


def test_iter_list_warns_when_the_safety_limit_truncates_a_list(monkeypatch):
    """A nonterminated list after the configured limit is reported."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    first = _FakeNode(0x200)
    second = _FakeNode(0x300)
    head.next = first
    first.next = second
    second.next = head

    assert list(layout.iter_list(head, _HOOK, max_count=1)) == [first]
    assert warnings == ["list traversal truncated after 1 nodes"]


def test_iter_list_does_not_warn_at_an_exact_safety_limit_boundary(monkeypatch):
    """Reaching the head at the limit is normal completion, not truncation."""
    warnings = _configure_iter_list(monkeypatch)
    head = _FakeNode(0x100)
    node = _FakeNode(0x200)
    head.next = node
    node.next = head

    assert list(layout.iter_list(head, _HOOK, max_count=1)) == [node]
    assert warnings == []
