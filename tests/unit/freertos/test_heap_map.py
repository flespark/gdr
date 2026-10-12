"""Unit tests for the FreeRTOS heap map behind ``frt heap dump`` (no GDB)."""

from __future__ import annotations

import types

import freertos.adapter as adapter_module
from freertos.heap import HeapBlock, HeapWalk


def _adapter():
    return adapter_module.FreeRtosAdapter(
        adapter_module.FreeRtosLayout(version=(10, 3, 1))
    )


def _snapshot(linear, algorithm="heap_4"):
    return types.SimpleNamespace(
        linear=linear, geometry=types.SimpleNamespace(algorithm=algorithm)
    )


def test_heap_map_draws_the_linear_walk_with_no_owners(monkeypatch):
    """Allocated and free blocks come from the linear walk; no owner names."""
    walk = HeapWalk(
        blocks=[
            HeapBlock(address=0x2000, size=64, allocated=True),
            HeapBlock(address=0x2040, size=32, allocated=False),
        ]
    )
    monkeypatch.setattr(
        adapter_module.heap_module,
        "heap_snapshot",
        lambda _layout: _snapshot(walk),
    )

    heap = _adapter().heap_map()

    assert heap is not None
    assert (heap.algorithm, heap.base, heap.end) == ("heap_4", 0x2000, 0x2060)
    assert [(b.address, b.size, b.used, b.owner) for b in heap.blocks] == [
        (0x2000, 64, True, None),
        (0x2040, 32, False, None),
    ]
    assert heap.owners is False


def test_heap_map_is_none_without_a_linear_walk(monkeypatch):
    """heap_3 and free-list-only walks have no allocated-block layout to draw."""
    monkeypatch.setattr(
        adapter_module.heap_module,
        "heap_snapshot",
        lambda _layout: _snapshot(None, algorithm="heap_3"),
    )
    assert _adapter().heap_map() is None


def test_heap_map_flags_a_truncated_walk(monkeypatch):
    """A bounded walk keeps its blocks and tells the reader later ones are missing."""
    walk = HeapWalk(
        blocks=[HeapBlock(address=0x2000, size=64, allocated=True)], truncated=True
    )
    monkeypatch.setattr(
        adapter_module.heap_module, "heap_snapshot", lambda _layout: _snapshot(walk)
    )

    heap = _adapter().heap_map()

    assert heap is not None
    assert heap.truncated is True
    assert "bound" in heap.messages[0]
