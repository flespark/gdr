"""Unit tests for FreeRTOS layout-driven pretty-printer folds.

The pretty-printer is driven entirely by ``StructLayout`` metadata; these
tests hold the FreeRTOS layouts to their promised one-line summary shape
without needing a live GDB target.
"""

from __future__ import annotations

import gdr.printers as printers
from freertos.layout import FreeRtosConfig, build_layout


def _fold(
    monkeypatch,
    struct_name: str,
    values: dict[str, object],
    config: FreeRtosConfig | None = None,
) -> str:
    """Render the one-line fold for *struct_name* with fake field values."""
    layout = build_layout(config or FreeRtosConfig(), (10, 3, 1))
    sl = layout.structs[struct_name]

    monkeypatch.setattr(printers, "read_field", lambda _v, _l, f: values.get(f))
    monkeypatch.setattr(printers, "read_cstring", lambda value: value)
    return printers.LayoutPrinter(object(), sl).to_string()


def test_task_fold_is_nonempty_and_names_expected_fields(monkeypatch):
    text = _fold(
        monkeypatch,
        "struct tskTaskControlBlock",
        {"name": "worker", "current_priority": 4},
    )
    assert text.startswith("Task(")
    assert "name=" in text
    assert "current_priority=" in text
    assert "worker" in text
    assert text != "Task()"


def test_queue_fold_is_nonempty_and_names_expected_fields(monkeypatch):
    text = _fold(
        monkeypatch,
        "struct QueueDefinition",
        {"length": 10, "count": 3, "type": 2},
        config=FreeRtosConfig(trace_facility=True),
    )
    assert text.startswith("Queue(")
    assert "length=" in text
    assert "count=" in text
    assert "type=counting-sem" in text
    assert text != "Queue()"


def test_queue_fold_omits_the_type_key_without_trace_facility(monkeypatch):
    """``ucQueueType`` is absent when configUSE_TRACE_FACILITY is 0.

    Describing it anyway would fold every queue as ``type=N/A`` on the default
    (trace-off) configuration instead of simply not claiming a type.
    """
    text = _fold(
        monkeypatch,
        "struct QueueDefinition",
        {"length": 10, "count": 3},
        config=FreeRtosConfig(trace_facility=False),
    )
    assert text == "Queue(length=10, count=3)"


def test_timer_fold_is_nonempty_and_names_expected_fields(monkeypatch):
    text = _fold(
        monkeypatch,
        "struct tmrTimerControl",
        {"name": "blink", "period": 1000},
    )
    assert text.startswith("Timer(")
    assert "name=" in text
    assert "period=" in text
    assert text != "Timer()"
