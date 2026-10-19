"""Fast unit tests for shared command rendering and error boundaries."""

from __future__ import annotations

from dataclasses import (
    dataclass,
    field,
)

import pytest

import core.commands as commands
import core.gdb_bridge as bridge
from core import commands as watch_module
from core.adapter import (
    ObjectDetail,
    ObjectTable,
    SystemSummary,
)
from core.commands import (
    WatchSpec,
    complete_watch,
)


@dataclass
class _Adapter:
    task_output: ObjectTable = field(default_factory=ObjectTable)
    counts: dict[str, int] = field(default_factory=dict)
    tables: dict[str, ObjectTable] = field(default_factory=dict)
    detail_tables: dict[str, ObjectTable] = field(default_factory=dict)
    details: dict[str, ObjectDetail] = field(default_factory=dict)
    summary: SystemSummary = field(default_factory=SystemSummary)
    count_calls: int = 0

    def iter_tasks(self):
        raise AssertionError("raw task iteration is not used for rendering")

    def task_table(self):
        return self.task_output

    def object_counts(self) -> dict[str, int]:
        self.count_calls += 1
        return self.counts

    def object_table(self, kind: str) -> ObjectTable | None:
        return self.tables.get(kind)

    def object_detail(self, kind: str, name: str) -> ObjectDetail | None:  # noqa: ARG002
        return self.details.get(kind)

    def detail_table(self, kind: str) -> ObjectTable | None:
        return self.detail_tables.get(kind)

    def system_summary(self) -> SystemSummary:
        return self.summary


def test_tasks_renders_adapter_owned_table(monkeypatch):
    """The shared renderer forwards an adapter-owned task table unchanged."""
    adapter = _Adapter(
        task_output=ObjectTable(
            headers=["Task", "Runtime"],
            rows=[["worker *", "0"]],
            messages=["snapshot complete"],
            elastic=("Task",),
        )
    )
    tables = []
    messages = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **kwargs: tables.append((rows, headers, kwargs)),
    )

    commands.render_tasks()

    assert messages == ["snapshot complete"]
    assert tables == [
        ([["worker *", "0"]], ["Task", "Runtime"], {"elastic": ("Task",)})
    ]


def test_tasks_renders_an_empty_adapter_without_target_access(monkeypatch):
    """An initialized adapter with no tasks still emits an empty table."""
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", _Adapter)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )

    commands.render_tasks()

    assert tables[0][0] == []


def test_tasks_warns_before_initialization(monkeypatch):
    """The renderer rejects use before an adapter has been selected."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: None)
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected table")),
    )

    commands.render_tasks()

    assert warnings == ["run `gdr init <rtos> <version>` first"]


def test_system_renders_summary_and_sorted_object_counts(monkeypatch):
    """The system renderer preserves normalized fields and stable count order."""
    adapter = _Adapter(
        counts={"timer": 2, "task": 3},
        summary=SystemSummary(
            current_task="worker",
            task_count=3,
            tick_count=123,
            scheduler_state="running",
            state_counts={"Ready": 2, "Suspended": 1},
            object_counts={"timer": 2, "task": 3},
        ),
    )
    written: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "print_detail", written.append)

    commands.render_system()

    assert written == [
        [
            ("Current task", "worker"),
            ("Task number", "3"),
            ("Tick count", "123"),
            ("Scheduler state", "running"),
            ("Ready", "2"),
            ("Suspended", "1"),
            ("task", "3"),
            ("timer", "2"),
            ("Heap allocator", "N/A"),
        ]
    ]
    assert adapter.count_calls == 0


def test_system_renders_heap_allocator_from_snapshot_fields(monkeypatch):
    """Used/total become their own aligned keys; status is adapter-owned."""
    adapter = _Adapter(
        summary=SystemSummary(
            heap_allocator="small_mem",
            heap_used=4096,
            heap_total=65536,
            heap_status="good",
        )
    )
    written: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "print_detail", written.append)

    commands.render_system()

    by_label = dict(written[-1])
    assert by_label["Heap allocator"] == "small_mem"
    assert by_label["Heap used"] == "4096 B"
    assert by_label["Heap total"] == "65536 B"
    assert by_label["Heap status"] == "good"


def test_system_renders_partial_heap_counters_as_na(monkeypatch):
    """A one-sided snapshot still names the allocator; the missing side is N/A."""
    adapter = _Adapter(
        summary=SystemSummary(
            heap_allocator="small_mem", heap_total=65536, heap_status="good"
        )
    )
    written: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "print_detail", written.append)

    commands.render_system()

    by_label = dict(written[-1])
    assert by_label["Heap allocator"] == "small_mem"
    assert by_label["Heap used"] == "N/A"
    assert by_label["Heap total"] == "65536 B"
    assert by_label["Heap status"] == "good"

    adapter.summary = SystemSummary(
        heap_allocator="small_mem", heap_used=0, heap_status="good"
    )
    written.clear()
    commands.render_system()
    by_label = dict(written[-1])
    assert by_label["Heap used"] == "0 B"
    assert by_label["Heap total"] == "N/A"


def test_system_renders_heap_status(monkeypatch):
    """The renderer prints the adapter's heap_status without re-deriving it."""
    adapter = _Adapter(
        summary=SystemSummary(
            heap_allocator="small_mem",
            heap_used=160,
            heap_total=256,
            heap_status="corrupt",
        )
    )
    written: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "print_detail", written.append)

    commands.render_system()
    by_label = dict(written[-1])
    assert by_label["Heap allocator"] == "small_mem"
    assert by_label["Heap used"] == "160 B"
    assert by_label["Heap total"] == "256 B"
    assert by_label["Heap status"] == "corrupt"
    assert "Heap source" not in by_label

    adapter.summary.heap_status = "overrun"
    written.clear()
    commands.render_system()
    assert dict(written[-1])["Heap status"] == "overrun"

    adapter.summary.heap_status = "good"
    written.clear()
    commands.render_system()
    assert dict(written[-1])["Heap status"] == "good"

    adapter.summary.heap_status = None
    written.clear()
    commands.render_system()
    assert "Heap status" not in dict(written[-1])


def test_objects_normalizes_kind_and_renders_adapter_table(monkeypatch):
    """Plural command names select the matching semantic object table."""
    adapter = _Adapter(
        counts={"timer": 1},
        tables={
            "timer": ObjectTable(
                headers=["Name", "Callback"],
                rows=[["heartbeat", "<tick>"]],
                messages=["Kernel tick: 10"],
                elastic=("Name", "Callback"),
            )
        },
    )
    messages: list[str] = []
    tables: list[tuple[list[list[str]], list[str], tuple[str, ...]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "info", messages.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append(
            (rows, headers, _kwargs.get("elastic", ()))
        ),
    )

    commands.render_objects("timer")

    assert messages == ["Kernel tick: 10"]
    assert tables == [
        ([["heartbeat", "<tick>"]], ["Name", "Callback"], ("Name", "Callback"))
    ]
    assert adapter.count_calls == 0


def test_objects_only_counts_when_no_detailed_table_is_available(monkeypatch):
    """Count traversal remains a fallback for kinds without detailed tables."""
    adapter = _Adapter(counts={"msgqueue": 2})
    tables: list[tuple[list[list[str]], list[str]]] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **_kwargs: tables.append((rows, headers)),
    )

    commands.render_objects("msgqueue")

    assert adapter.count_calls == 1
    assert tables == [([["msgqueue", "2"]], ["Kind", "Count"])]


def test_shared_renderer_guard_contains_unexpected_adapter_errors(monkeypatch):
    """A broken adapter cannot leak a Python exception through a GDB command."""
    errors: list[str] = []

    class _BrokenAdapter(_Adapter):
        def task_table(self):
            raise ValueError("corrupt task list")

    monkeypatch.setattr(commands, "active", _BrokenAdapter)
    monkeypatch.setattr(bridge, "err", errors.append)
    monkeypatch.setattr(bridge, "is_debug", lambda: False)

    assert commands.render_tasks() is None
    assert errors == ["render_tasks: ValueError: corrupt task list"]


def test_object_detail_selects_one_object_even_when_panel_tables_exist(monkeypatch):
    """CLI detail never collects the all-object Panel table or truncates entries."""
    pairs = [
        ("Name", "test_sem"),
        ("Value", "3"),
        ("Check[ListCount]", "fail: " + "long diagnostic evidence " * 12),
        ("Waiter[0]", "worker"),
    ]
    adapter = _Adapter(details={"semaphore": ObjectDetail(pairs=pairs)})
    selected = []
    written = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        adapter,
        "detail_table",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("unexpected all-object traversal")
        ),
    )
    monkeypatch.setattr(
        adapter,
        "object_detail",
        lambda kind, name: selected.append((kind, name)) or adapter.details[kind],
    )
    monkeypatch.setattr(commands, "print_detail", written.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("unexpected table")),
    )

    commands.render_object_detail(" SEMAPHORE ", "test_sem")

    assert selected == [("semaphore", "test_sem")]
    assert written == [pairs]


def test_object_detail_renders_the_vertical_block(monkeypatch):
    """The selected object is printed as ordered Key: Value pairs."""
    adapter = _Adapter(
        details={
            "semaphore": ObjectDetail(pairs=[("Name", "test_sem"), ("Value", "3")])
        }
    )
    written: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "print_detail", written.append)

    commands.render_object_detail("semaphore", "test_sem")

    assert written == [[("Name", "test_sem"), ("Value", "3")]]


def test_object_detail_warns_when_kind_is_not_enumerable(monkeypatch):
    """Adapters that cannot enumerate a kind produce an explicit warning."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_detail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected detail")),
    )

    commands.render_object_detail("mempool", "x")

    assert warnings == ["object kind 'mempool' is not reliably enumerable"]


def test_object_detail_warns_when_object_is_missing(monkeypatch):
    """A missing or disabled object reports a clear not-found diagnostic."""
    adapter = _Adapter(details={"semaphore": ObjectDetail(found=False)})
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("unexpected table")),
    )

    commands.render_object_detail("semaphore", "missing")

    assert warnings == ["semaphore 'missing': not found or type not enabled"]


def test_object_detail_preserves_address_selection_and_type_redirect(monkeypatch):
    adapter = _Adapter()
    selected = []
    message = "'0x2000' is a mutex, not a semaphore; try `freertos mutex 0x2000`"
    monkeypatch.setattr(commands, "active", lambda: adapter)
    monkeypatch.setattr(
        adapter,
        "object_detail",
        lambda kind, name: (
            selected.append((kind, name)) or ObjectDetail(found=False, message=message)
        ),
    )
    warnings = []
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands.render_object_detail("semaphore", "0x2000")

    assert selected == [("semaphore", "0x2000")]
    assert warnings == [message]


def test_object_detail_warns_before_initialization(monkeypatch):
    """The detail renderer rejects use before an adapter has been selected."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: None)
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands.render_object_detail("semaphore", "test_sem")

    assert warnings == ["run `gdr init <rtos> <version>` first"]


def test_object_detail_warns_for_an_empty_name(monkeypatch):
    """A missing object name receives a generic renderer diagnostic."""
    warnings: list[str] = []
    monkeypatch.setattr(commands, "active", lambda: _Adapter())
    monkeypatch.setattr(commands, "warn", warnings.append)
    monkeypatch.setattr(
        commands,
        "print_detail",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected detail")),
    )

    commands.render_object_detail("semaphore", "  ")

    assert warnings == ["object name or address must not be empty"]


def test_heap_dump_options_defaults_to_bare_dump():
    """No tail words mean the default bitmap with no output path."""
    options, error = commands.render_heap_dump_options([], "usage: x")

    assert error is None
    assert options == commands.HeapDumpOptions(path="", cell_px=None, max_px=None)


def test_heap_dump_options_takes_path_and_both_flag_spellings():
    """A bare path plus space- and equals-form flags parse together."""
    options, error = commands.render_heap_dump_options(
        ["out.png", "--cell-pixel", "20", "--width=1600"], "usage: x"
    )

    assert error is None
    assert options == commands.HeapDumpOptions(path="out.png", cell_px=20, max_px=1600)


def test_heap_dump_options_rejects_shapes_values_and_mismatches():
    """Unknown flags, missing values and a width narrower than one cell fail."""
    usage = "usage: x"
    assert commands.render_heap_dump_options(["--bogus"], usage) == (None, usage)
    assert commands.render_heap_dump_options(["--cell-pixel"], usage) == (None, usage)
    assert commands.render_heap_dump_options(["a.png", "b.png"], usage) == (None, usage)
    assert commands.render_heap_dump_options(["--width", "x"], usage)[0] is None
    assert commands.render_heap_dump_options(["--cell-pixel", "0"], usage)[0] is None
    assert commands.render_heap_dump_options(["--cell-pixel", "65"], usage)[0] is None
    options, error = commands.render_heap_dump_options(
        ["--width", "5", "--cell-pixel", "10"], usage
    )
    assert options is None
    assert error == "heap bitmap width 5 is narrower than one 10-pixel cell"


def test_heap_dump_options_takes_row_bytes():
    """Row-exact tiling composes with the other sizing flags."""
    options, error = commands.render_heap_dump_options(
        ["--row-bytes", "1024"], "usage: x"
    )

    assert error is None
    assert options == commands.HeapDumpOptions(
        path="", cell_px=None, max_px=None, row_bytes=1024
    )
    bad, error = commands.render_heap_dump_options(["--row-bytes", "0"], "usage: x")
    assert bad is None
    assert error is not None


class _WatchAdapter:
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
    adapter = _WatchAdapter()
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
