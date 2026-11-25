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


def test_complete_watch_supports_backtrace_targets_and_max_depth():
    spec = WatchSpec(
        kind_words={"threads": "task"},
        singular_kinds={"thread": "task"},
        aliases={"bt": "backtrace"},
        object_names_fn=lambda kind: ["worker1", "idle"] if kind == "task" else [],
        backtrace_word="backtrace",
    )
    assert complete_watch(spec, "watch", None) == ["threads", "thread", "backtrace"]
    assert complete_watch(spec, "watch back", "back") == ["backtrace"]
    assert complete_watch(spec, "watch backtrace ", "") == ["worker1", "idle"]
    assert complete_watch(spec, "watch bt w", "w") == ["worker1"]
    assert complete_watch(spec, "watch bt --", "--") == ["--max-depth"]
    assert complete_watch(spec, "watch bt --max-depth ", "") == []
    assert complete_watch(spec, "watch bt worker1 --max-depth 8 ", "") == []


def test_format_backtrace_watch_label():
    assert commands.format_backtrace_watch_label("rtt", "bt", None) == "rtt bt"
    assert (
        commands.format_backtrace_watch_label("frt", "backtrace", "Tmr Svc", 8)
        == "frt backtrace Tmr Svc --max-depth 8"
    )
    assert (
        commands.format_backtrace_watch_label("", "BT", "worker1", 32) == "bt worker1"
    )


class _BacktraceWatchAdapter(_WatchAdapter):
    """Watch adapter stand-in with concrete ``backtrace`` support."""

    def __init__(self) -> None:
        super().__init__()
        self.bt_calls: list[tuple[str | None, int]] = []
        self.bt_reports: dict[str | None, object] = {}

    def backtrace(self, target: str | None = None, max_depth: int = 32):
        self.bt_calls.append((target, max_depth))
        return self.bt_reports.get(target)


def test_add_backtrace_watch_validates_support_depth_and_target(adapter, monkeypatch):
    _plain_adapter, _printed, _messages = adapter
    assert (
        watch_module.add_watch("rtt bt", "backtrace", None)
        == "this adapter cannot unwind task stacks"
    )

    from core.adapter import RtosAdapter

    class _BaseOnlyAdapter(_WatchAdapter):
        backtrace = RtosAdapter.backtrace

    monkeypatch.setattr(watch_module, "active", lambda: _BaseOnlyAdapter())
    assert (
        watch_module.add_watch("rtt bt", "backtrace", None)
        == "this adapter cannot unwind task stacks"
    )

    bt_adapter = _BacktraceWatchAdapter()
    bt_adapter.objects[("task", "worker")] = object()
    monkeypatch.setattr(watch_module, "active", lambda: bt_adapter)

    assert (
        watch_module.add_watch("rtt bt --max-depth 0", "backtrace", None, max_depth=0)
        == "--max-depth must be between 1 and 64"
    )
    assert (
        watch_module.add_watch("rtt bt --max-depth 65", "backtrace", None, max_depth=65)
        == "--max-depth must be between 1 and 64"
    )
    assert (
        watch_module.add_watch("rtt bt", "backtrace", "   ")
        == "task name or address must not be empty"
    )
    assert (
        watch_module.add_watch("rtt bt missing", "backtrace", "missing")
        == "task 'missing': not found or type not enabled"
    )

    assert watch_module.add_watch("rtt bt", "backtrace", None, max_depth=16) is None
    assert (
        watch_module.add_watch("rtt backtrace", "backtrace", None, max_depth=32)
        == "already watching rtt bt"
    )
    assert (
        watch_module.add_watch("rtt bt worker", "backtrace", "worker", max_depth=8)
        is None
    )
    assert watch_module.list_watches() == [
        watch_module.Watch(kind="backtrace", target=None, label="rtt bt", max_depth=16),
        watch_module.Watch(
            kind="backtrace", target="worker", label="rtt bt worker", max_depth=8
        ),
    ]
    assert watch_module.remove_watch("backtrace", "worker") is True
    assert watch_module.remove_watch("backtrace", "worker") is False
    assert watch_module.remove_watch("backtrace", None) is True


def test_render_backtrace_watches_covers_normal_gone_ambiguous_and_skipped(
    adapter, monkeypatch
):
    from core.adapter import BacktraceFrame, BacktraceReport

    _plain, _printed, messages = adapter
    bt_adapter = _BacktraceWatchAdapter()
    bt_adapter.objects[("task", "worker")] = object()
    bt_adapter.objects[("task", "gone")] = object()
    bt_adapter.objects[("task", "dup")] = object()
    bt_adapter.objects[("task", "none")] = object()
    monkeypatch.setattr(watch_module, "active", lambda: bt_adapter)

    rendered_reports: list[BacktraceReport] = []
    monkeypatch.setattr(
        watch_module, "render_backtrace_report", rendered_reports.append
    )

    normal_report = BacktraceReport(
        task="worker",
        address=0x20001000,
        architecture="armv7-m",
        method="saved-context",
        source="saved",
        complete=True,
        stop_reason="task-exit",
        truncated=False,
        max_depth=12,
        bounds="recorded",
        frames=[BacktraceFrame(index=0, pc=0x08001000, method="seed", symbol="work")],
    )
    bt_adapter.bt_reports[None] = normal_report
    bt_adapter.bt_reports["gone"] = BacktraceReport(
        task="gone",
        address=0,
        architecture="armv7-m",
        method="unavailable",
        source="saved",
        complete=False,
        stop_reason="task-not-found",
        truncated=False,
        max_depth=32,
        bounds="none",
        frames=[],
        messages=[],
    )
    bt_adapter.bt_reports["dup"] = BacktraceReport(
        task="dup",
        address=0,
        architecture="armv7-m",
        method="unavailable",
        source="saved",
        complete=False,
        stop_reason="ambiguous-target",
        truncated=False,
        max_depth=32,
        bounds="none",
        frames=[],
        messages=["task 'dup': matches 2 objects"],
    )
    bt_adapter.bt_reports["none"] = None

    assert (
        watch_module.add_watch("rtt bt --max-depth 12", "backtrace", None, max_depth=12)
        is None
    )
    assert watch_module.add_watch("rtt bt gone", "backtrace", "gone") is None
    assert watch_module.add_watch("rtt bt dup", "backtrace", "dup") is None
    assert watch_module.add_watch("rtt bt none", "backtrace", "none") is None

    watch_module.render_watches()

    assert bt_adapter.bt_calls == [
        (None, 12),
        ("gone", 32),
        ("dup", 32),
        ("none", 32),
    ]
    assert rendered_reports == [normal_report]
    assert "[gdr watch: rtt bt --max-depth 12]" in messages
    assert "[gdr watch: rtt bt gone] gone (detached?)" in messages
    assert "[gdr watch: rtt bt dup] task 'dup': matches 2 objects" in messages
    assert (
        "[gdr watch: rtt bt none] skipped: this adapter cannot unwind task stacks"
        in messages
    )


def test_invoke_backtrace_command_supports_omitted_and_explicit_targets(monkeypatch):
    calls: list[tuple[str | None, int]] = []
    warnings: list[str] = []
    monkeypatch.setattr(
        commands,
        "render_backtrace",
        lambda target=None, max_depth=32: calls.append((target, max_depth)),
    )
    monkeypatch.setattr(commands, "warn", warnings.append)

    commands.invoke_backtrace_command("bt", "usage: test")
    commands.invoke_backtrace_command("bt --max-depth 8", "usage: test")
    commands.invoke_backtrace_command("bt --max-depth=12", "usage: test")
    commands.invoke_backtrace_command("backtrace Tmr Svc --max-depth 6", "usage: test")
    commands.invoke_backtrace_command("bt --max-depth", "usage: test")
    commands.invoke_backtrace_command("bt --unknown", "usage: test")
    commands.invoke_backtrace_command("bt --max-depth abc", "usage: test")

    assert calls == [(None, 32), (None, 8), (None, 12), ("Tmr Svc", 6)]
    assert warnings == ["usage: test", "usage: test", "invalid --max-depth: abc"]


def test_render_backtrace_report_gates_logs_on_debug(monkeypatch, capsys):
    from core.adapter import BacktraceFrame, BacktraceReport
    from core.formatting import SourceLoc

    report = BacktraceReport(
        task="gdr_spin",
        address=0x280033A0,
        architecture="armv8-m.mainline",
        method="exception",
        source="current",
        complete=True,
        stop_reason="task-exit",
        truncated=False,
        max_depth=32,
        bounds="recorded",
        frames=[
            BacktraceFrame(
                index=0,
                pc=0x10002910,
                method="seed",
                symbol="SysTick_Handler",
                source=SourceLoc(
                    "/Users/simon/Source/FreeRTOS-Kernel/portable/GCC/ARM_CM33_NTZ/non_secure/port.c",
                    1078,
                    "/Users/simon/Source/FreeRTOS-Kernel/portable/GCC/ARM_CM33_NTZ/non_secure/port.c",
                ),
            )
        ],
        messages=["exception transition exception:15 -> task:0x280033a0 at 0x100028bc"],
    )
    infos: list[str] = []
    monkeypatch.setattr(commands, "info", infos.append)
    tables: list[tuple[list[list[str]], list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **kwargs: tables.append((rows, headers, kwargs)),
    )
    monkeypatch.setattr(
        commands,
        "format_source_loc",
        lambda source: (
            f"portable/GCC/ARM_CM33_NTZ/non_secure/port.c:{source.line}"
            if source is not None
            else ""
        ),
    )

    monkeypatch.setattr(commands, "is_debug", lambda: False)
    commands.render_backtrace_report(report)
    assert infos == []
    assert tables[-1] == (
        [
            [
                "0",
                "0x10002910",
                "SysTick_Handler",
                "portable/GCC/ARM_CM33_NTZ/non_secure/port.c:1078",
                "seed",
            ]
        ],
        ["#", "PC", "Function", "Source", "Method"],
        {"elastic": {"Source": "right"}},
    )
    assert "gdr_spin: armv8-m.mainline, current, exception, complete (task-exit)" in (
        capsys.readouterr().out
    )

    monkeypatch.setattr(commands, "is_debug", lambda: True)
    commands.render_backtrace_report(report)
    assert infos == [
        "exception transition exception:15 -> task:0x280033a0 at 0x100028bc"
    ]


def test_render_object_table_shortens_file_columns_and_passes_modes(monkeypatch):
    from core.adapter import ColumnSpec, ObjectTable, text_cell

    tables: list[tuple[list[list[str]], list[str], dict[str, object]]] = []
    monkeypatch.setattr(
        commands, "display_path", lambda path: "src/" + path.rsplit("/", 1)[-1]
    )
    monkeypatch.setattr(
        commands,
        "print_table",
        lambda rows, headers, **kwargs: tables.append((rows, headers, kwargs)),
    )
    table = ObjectTable.from_specs(
        (
            ColumnSpec("Name", elastic="left"),
            ColumnSpec("File", kind="file", elastic="right"),
        ),
        [[text_cell("worker"), text_cell("/abs/ws/src/main.c")]],
    )
    commands._render_object_table(table)
    assert tables == [
        (
            [["worker", "src/main.c"]],
            ["Name", "File"],
            {
                "elastic": ("Name", "File"),
                "elastic_modes": {"Name": "left", "File": "right"},
            },
        )
    ]
