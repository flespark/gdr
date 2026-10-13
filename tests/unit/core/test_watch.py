"""Unit tests for the stop-time watch surface (``<rtos> watch``)."""

from __future__ import annotations

import pytest

from gdr import watch as watch_module
from gdr.adapter_api import ObjectDetail, ObjectTable
from gdr.watch import WatchSpec, complete_watch


class _Adapter:
    """Minimal adapter stand-in behind the watch collectors."""

    def __init__(self) -> None:
        self.counts = {"task": 1, "semaphore": 1}
        self.objects: dict[tuple[str, str], object | None] = {}
        self.details: dict[tuple[str, str], ObjectDetail | None] = {}
        self.unreadable: set[tuple[str, str]] = set()

    def object_counts(self) -> dict[str, int]:
        return dict(self.counts)

    def task_table(self) -> ObjectTable:
        return ObjectTable(headers=["Name"], rows=[["worker"]])

    def object_table(self, kind: str) -> ObjectTable | None:
        if kind == "semaphore":
            return ObjectTable(headers=["Name"], rows=[["work_sem"]])
        return None

    def object_detail(self, kind: str, target: str) -> ObjectDetail | None:
        return self.details.get((kind, target))

    def find_task(self, target: str):
        return self._find(("task", target))

    def find_object(self, kind: str, target: str):
        return self._find((kind, target))

    def _find(self, key: tuple[str, str]):
        if key in self.unreadable:
            raise ValueError("bad target read")
        return self.objects.get(key)


@pytest.fixture
def adapter(monkeypatch):
    watch_module.clear()
    adapter = _Adapter()
    monkeypatch.setattr(watch_module, "active", lambda: adapter)
    printed: list[tuple] = []
    monkeypatch.setattr(
        watch_module,
        "print_table",
        lambda rows, headers, **_kw: printed.append(("table", rows, headers)),
    )
    monkeypatch.setattr(
        watch_module, "print_detail", lambda pairs: printed.append(("detail", pairs))
    )
    messages: list[str] = []
    monkeypatch.setattr(watch_module, "info", messages.append)
    return adapter, printed, messages


def test_add_kind_watch_validates_enumerability(adapter):
    adapter, _printed, _messages = adapter
    assert watch_module.add_watch("rtt threads", "task", None) is None
    assert watch_module.list_watches() == [
        watch_module.Watch(kind="task", target=None, label="rtt threads")
    ]
    assert (
        watch_module.add_watch("rtt heap", "heap", None)
        == "object kind 'heap' is not reliably enumerable"
    )
    assert len(watch_module.list_watches()) == 1


def test_add_singleton_requires_a_live_object(adapter):
    adapter, _printed, _messages = adapter
    adapter.objects[("semaphore", "work_sem")] = object()
    assert (
        watch_module.add_watch("rtt semaphore work_sem", "semaphore", "work_sem")
        is None
    )
    assert (
        watch_module.add_watch("rtt semaphore missing", "semaphore", "missing")
        == "semaphore 'missing': not found or type not enabled"
    )
    adapter.unreadable.add(("semaphore", "broken"))
    assert watch_module.add_watch(
        "rtt semaphore broken", "semaphore", "broken"
    ).endswith("(target halted?)")
    assert (
        watch_module.add_watch("rtt semaphore work_sem", "semaphore", "work_sem")
        == "already watching rtt semaphore work_sem"
    )


def test_add_without_an_adapter_names_init(adapter, monkeypatch):
    _adapter, _printed, _messages = adapter
    monkeypatch.setattr(watch_module, "active", lambda: None)
    assert watch_module.add_watch("rtt threads", "task", None).startswith(
        "run `gdr init"
    )


def test_remove_and_clear(adapter):
    adapter, _printed, _messages = adapter
    watch_module.add_watch("rtt threads", "task", None)
    assert watch_module.remove_watch("task", None) is True
    assert watch_module.remove_watch("task", None) is False
    watch_module.add_watch("rtt threads", "task", None)
    watch_module.clear()
    assert watch_module.list_watches() == []


def test_render_watch_list_states(adapter):
    _adapter, _printed, messages = adapter
    watch_module.render_watch_list()
    assert messages == ["no watches (add one with `<rtos> watch <kind>`)"]
    watch_module.add_watch("rtt threads", "task", None)
    watch_module.render_watch_list()
    assert messages[-1] == "[watch 0] rtt threads"


def test_render_watches_is_silent_when_empty(adapter, monkeypatch):
    _adapter, printed, _messages = adapter
    watch_module.render_watches()
    assert printed == []
    monkeypatch.setattr(watch_module, "active", lambda: None)
    watch_module.add_watch("rtt threads", "task", None)
    watch_module.render_watches()
    assert printed == []


def test_render_kind_and_singleton_blocks(adapter):
    adapter, printed, messages = adapter
    adapter.objects[("semaphore", "work_sem")] = object()
    adapter.details[("semaphore", "work_sem")] = ObjectDetail(
        pairs=[("Name", "work_sem")]
    )
    watch_module.add_watch("rtt threads", "task", None)
    watch_module.add_watch("rtt semaphores", "semaphore", None)
    watch_module.add_watch("rtt semaphore work_sem", "semaphore", "work_sem")

    watch_module.render_watches()

    assert ("table", [["worker"]], ["Name"]) in printed
    assert ("table", [["work_sem"]], ["Name"]) in printed
    assert ("detail", [("Name", "work_sem")]) in printed
    assert "[gdr watch: rtt threads]" in messages
    assert "[gdr watch: rtt semaphore work_sem]" in messages


def test_render_gone_and_stale_entries(adapter):
    adapter, printed, messages = adapter
    adapter.objects[("semaphore", "work_sem")] = object()
    adapter.details[("semaphore", "work_sem")] = ObjectDetail(found=False)
    watch_module.add_watch("rtt semaphore work_sem", "semaphore", "work_sem")
    watch_module.add_watch("rtt threads", "task", None)
    del adapter.counts["task"]

    watch_module.render_watches()

    assert printed == []
    assert "[gdr watch: rtt semaphore work_sem] gone (detached?)" in messages
    assert "[gdr watch: rtt threads] skipped: kind not enumerable now" in messages


def test_render_one_bad_entry_cannot_silence_the_rest(adapter, monkeypatch):
    adapter, printed, _messages = adapter
    adapter.objects[("semaphore", "work_sem")] = object()
    adapter.details[("semaphore", "work_sem")] = ObjectDetail(
        pairs=[("Name", "work_sem")]
    )

    def boom_table(_adapter):
        raise RuntimeError("transient walk failure")

    monkeypatch.setattr(watch_module, "collect_task_table", boom_table)
    watch_module.add_watch("rtt threads", "task", None)
    watch_module.add_watch("rtt semaphore work_sem", "semaphore", "work_sem")

    watch_module.render_watches()

    assert ("detail", [("Name", "work_sem")]) in printed


def test_complete_watch_offers_kinds_then_live_names():
    spec = WatchSpec(
        kind_words={"threads": "task", "semaphores": "semaphore"},
        singular_kinds={"thread": "task", "semaphore": "semaphore"},
        aliases={"sem": "semaphore"},
        object_names_fn=lambda kind: ["work_sem"] if kind == "semaphore" else [],
    )
    assert complete_watch(spec, "watch", None) == [
        "threads",
        "semaphores",
        "thread",
        "semaphore",
    ]
    assert complete_watch(spec, "watch sem", "sem") == ["semaphores", "semaphore"]
    assert complete_watch(spec, "watch semaphore ", "") == ["work_sem"]
    assert complete_watch(spec, "watch sem ", "") == ["work_sem"]
