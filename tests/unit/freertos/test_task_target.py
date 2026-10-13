"""FreeRTOS task detail accepts the unified name/address/symbol selector."""

from __future__ import annotations

import freertos.adapter as adapter_module
from freertos.layout import FreeRtosLayout


def test_task_detail_falls_back_to_an_explicit_address(monkeypatch):
    raw = object()
    task = adapter_module.FreeRtosTask(
        name="worker", address=0x2000, state="Running", current_priority=3
    )
    monkeypatch.setattr(adapter_module, "find_task", lambda _target, _layout: None)
    monkeypatch.setattr(
        adapter_module,
        "resolve_object",
        lambda kind, text, _layout: (
            adapter_module.DiscoveredObject(kind=kind, address=0x2000, source="user")
            if (kind, text) == ("task", "0x2000")
            else None
        ),
    )
    monkeypatch.setattr(
        adapter_module, "_cast_object", lambda _address, _kind, _layout: raw
    )
    monkeypatch.setattr(
        adapter_module, "task_state", lambda _value, _layout: ("Running", 0)
    )
    monkeypatch.setattr(
        adapter_module,
        "value_to_task",
        lambda _value, _state, _core, _layout: task,
    )
    monkeypatch.setattr(
        adapter_module,
        "task_detail",
        lambda converted, _layout, **_kwargs: [("Name", converted.name)],
    )
    adapter = adapter_module.FreeRtosAdapter(FreeRtosLayout(version=(10, 3, 1)))

    detail = adapter.object_detail("task", "0x2000")

    assert detail is not None
    assert detail.pairs == [("Name", "worker")]
