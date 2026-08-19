"""Unit tests for RT-Thread navigation fallbacks."""

from __future__ import annotations

import rtthread.navigation as navigation
from gdr.layout import StructLayout


class _FakeType:
    """Minimal GDB type stand-in for CPU table shape tests."""

    def __init__(self, code: int):
        self.code = code

    def strip_typedefs(self):
        """Return the unaliased type used by navigation."""
        return self


class _FakeValue:
    """Minimal GDB value stand-in with array-style indexing."""

    def __init__(self, type_code: int, entries: list[object], address: int = 1):
        self.type = _FakeType(type_code)
        self._entries = entries
        self._address = address

    def __getitem__(self, index: int) -> object:
        return self._entries[index]

    def __int__(self) -> int:
        return self._address


class _FakeGdb:
    """GDB constants and exceptions used by ``_cpu_from_table``."""

    TYPE_CODE_ARRAY = 1
    TYPE_CODE_PTR = 2

    class error(Exception):
        pass

    class MemoryError(Exception):
        pass


def test_cpu_table_fallback_accepts_array_and_pointer_shapes(monkeypatch):
    """RT-Thread branches may expose either CPU table representation."""
    monkeypatch.setattr(navigation, "gdb", _FakeGdb)

    array_entries = [object(), object()]
    pointer_entries = [object(), object()]
    array_table = _FakeValue(_FakeGdb.TYPE_CODE_ARRAY, array_entries)
    pointer_table = _FakeValue(_FakeGdb.TYPE_CODE_PTR, pointer_entries)

    assert navigation._cpu_from_table(array_table, 1) is array_entries[1]
    assert navigation._cpu_from_table(pointer_table, 1) is pointer_entries[1]
    assert navigation._cpu_from_table(None, 0) is None


def test_find_thread_uses_the_active_layout_object_code(monkeypatch):
    """3.1.0's thread code is resolved from the selected version profile."""
    calls: list[tuple[int, str]] = []
    layout = navigation.KernelLayout(object_codes={"thread": 0})

    def find_object(type_code, name, _layout):
        calls.append((type_code, name))
        return "thread-value"

    monkeypatch.setattr(navigation, "find_object", find_object)

    assert navigation.find_thread("worker1", layout) == "thread-value"
    assert calls == [(0, "worker1")]


def test_suspend_thread_names_recovers_thread_names_via_tlist(monkeypatch):
    """Waiters are recovered through ``struct rt_thread.tlist`` nodes."""
    layout = navigation.KernelLayout(
        structs={
            "struct rt_semaphore": StructLayout("struct rt_semaphore"),
            "struct rt_thread": StructLayout("struct rt_thread"),
        }
    )
    head = object()
    waiter_values = [object(), object()]
    read_calls: list[str] = []

    def fake_read_field(value, sl, field_name):
        read_calls.append(field_name)
        if sl is layout.structs["struct rt_semaphore"]:
            return head
        return value

    monkeypatch.setattr(navigation, "read_field", fake_read_field)
    monkeypatch.setattr(
        navigation,
        "iter_suspend_threads",
        lambda _head: iter(waiter_values),
    )
    monkeypatch.setattr(
        navigation,
        "read_cstring",
        lambda value: f"name-{id(value)}",
    )

    names = navigation.suspend_thread_names(
        object(), layout, "struct rt_semaphore", "suspend_thread"
    )

    assert names == [f"name-{id(value)}" for value in waiter_values]
    assert read_calls == ["suspend_thread", "name", "name"]


def test_suspend_thread_names_renders_invalid_for_unreadable_names(monkeypatch):
    """An unreadable waiter name renders as ``<invalid>``, never crashes."""
    layout = navigation.KernelLayout(
        structs={
            "struct rt_semaphore": StructLayout("struct rt_semaphore"),
            "struct rt_thread": StructLayout("struct rt_thread"),
        }
    )
    monkeypatch.setattr(navigation, "read_field", lambda _v, _sl, _f: object())
    monkeypatch.setattr(
        navigation, "iter_suspend_threads", lambda _head: iter([object()])
    )
    monkeypatch.setattr(navigation, "read_cstring", lambda _value: None)

    names = navigation.suspend_thread_names(
        object(), layout, "struct rt_semaphore", "suspend_thread"
    )

    assert names == ["<invalid>"]


def test_suspend_thread_names_returns_empty_when_layout_missing():
    """A missing struct or head field degrades to an empty waiter list."""
    layout = navigation.KernelLayout()
    assert (
        navigation.suspend_thread_names(
            object(), layout, "struct rt_semaphore", "suspend_thread"
        )
        == []
    )


def test_iter_suspend_threads_uses_tlist_container_hook(monkeypatch):
    """The suspend hook recovers threads from their embedded ``tlist`` node."""
    head = object()
    captured: list[tuple[object, object]] = []

    def fake_iter_list(head_value, hook, max_count):
        captured.append((head_value, hook))
        assert hook.node_path == ("tlist",)
        assert hook.container_type == "struct rt_thread"
        assert hook.next_path == ("next",)
        assert max_count == navigation.MAX_SUSPEND_THREADS
        return iter([object()])

    monkeypatch.setattr(navigation, "iter_list", fake_iter_list)

    result = list(navigation.iter_suspend_threads(head))

    assert len(result) == 1
    assert captured == [(head, object())] or len(captured) == 1


def test_iter_object_names_skips_missing_type_code():
    """An unknown semantic kind yields no names."""
    layout = navigation.KernelLayout()

    assert list(navigation.iter_object_names("device", layout)) == []


def test_iter_object_names_disabled_type_yields_nothing(monkeypatch):
    """A type the target does not compile is skipped for completion."""
    from gdr.layout import ObjectTypeInfo

    layout = navigation.KernelLayout(
        object_codes={"semaphore": 2},
        object_types={
            2: ObjectTypeInfo(
                2,
                "struct rt_semaphore",
                ("parent", "parent", "list"),
                ("next",),
                enabled=False,
                name="semaphore",
            )
        },
    )
    monkeypatch.setattr(
        navigation,
        "iter_objects",
        lambda _code, _layout: (_ for _ in ()).throw(AssertionError("unused")),
    )

    assert list(navigation.iter_object_names("semaphore", layout)) == []


def test_iter_object_names_walks_the_object_registry(monkeypatch):
    """Completion names come from a live kernel-registry traversal."""
    from gdr.layout import ObjectTypeInfo

    layout = navigation.KernelLayout(
        object_codes={"semaphore": 2},
        structs={"struct rt_semaphore": StructLayout("struct rt_semaphore")},
        object_types={
            2: ObjectTypeInfo(
                2,
                "struct rt_semaphore",
                ("parent", "parent", "list"),
                ("next",),
                enabled=True,
                name="semaphore",
            )
        },
    )
    values = [object(), object(), object()]
    names_by_id = {
        id(v): n for v, n in zip(values, ["test_sem", "other", "third"], strict=False)
    }
    monkeypatch.setattr(navigation, "iter_objects", lambda _code, _layout: iter(values))
    monkeypatch.setattr(navigation, "read_field", lambda value, _sl, _f: value)
    monkeypatch.setattr(
        navigation, "read_cstring", lambda value: names_by_id.get(id(value))
    )

    assert list(navigation.iter_object_names("semaphore", layout)) == [
        "test_sem",
        "other",
        "third",
    ]


def test_iter_object_names_filters_unreadable_and_task_mapping(monkeypatch):
    """Task maps to thread and unreadable names are dropped."""
    from gdr.layout import ObjectTypeInfo

    layout = navigation.KernelLayout(
        object_codes={"thread": 1},
        structs={"struct rt_thread": StructLayout("struct rt_thread")},
        object_types={
            1: ObjectTypeInfo(
                1, "struct rt_thread", ("list",), ("next",), enabled=True, name="thread"
            )
        },
    )
    values = [object()]
    monkeypatch.setattr(navigation, "iter_objects", lambda _code, _layout: iter(values))
    monkeypatch.setattr(navigation, "read_field", lambda _value, _sl, _f: _value)
    monkeypatch.setattr(navigation, "read_cstring", lambda _value: None)

    assert list(navigation.iter_object_names("task", layout)) == []


def test_iter_object_names_uses_timer_traversal(monkeypatch):
    """Timers complete from the live timer list, not the object registry."""
    layout = navigation.KernelLayout(
        structs={"struct rt_timer": StructLayout("struct rt_timer")}
    )
    timers = [object(), object()]
    names_by_id = {
        id(v): n for v, n in zip(timers, ["heartbeat", "watchdog"], strict=False)
    }
    monkeypatch.setattr(navigation, "iter_timers", lambda _layout: iter(timers))
    monkeypatch.setattr(navigation, "read_field", lambda value, _sl, _f: value)
    monkeypatch.setattr(
        navigation, "read_cstring", lambda value: names_by_id.get(id(value))
    )

    assert list(navigation.iter_object_names("timer", layout)) == [
        "heartbeat",
        "watchdog",
    ]


class _Ptr:
    """Minimal GDB pointer stand-in for current-thread and tick tests."""

    def __init__(self, address: int, value=None):
        self._address = address
        self._value = value

    def __int__(self) -> int:
        return self._address

    def dereference(self):
        return self._value


class _Cpu:
    """Minimal ``struct rt_cpu`` stand-in."""

    def __init__(self, *, tick=None, current_thread=None, type_code: int = 0):
        self.type = _FakeType(type_code)
        self._tick = tick
        self._current_thread = current_thread

    def __getitem__(self, key: str):
        if key == "tick":
            return self._tick
        if key == "current_thread":
            return self._current_thread
        raise KeyError(key)


class _CpuGdb(_FakeGdb):
    """GDB stand-in exposing a selected frame and thread."""

    def __init__(self, frame=None, thread=None):
        self._frame = frame
        self._thread = thread

    def selected_frame(self):
        if self._frame is None:
            raise self.error("no frame")
        return self._frame

    def selected_thread(self):
        return self._thread


class _Frame:
    def __init__(self, registers: dict[str, int]):
        self._registers = registers

    def read_register(self, name: str) -> int:
        if name not in self._registers:
            raise _FakeGdb.error(f"no register {name}")
        return self._registers[name]


class _Thread:
    def __init__(self, num: int):
        self.num = num


def test_get_object_information_scans_the_static_container(monkeypatch):
    """Object registry lookup never calls ``rt_object_get_information()``."""
    from gdr.layout import KernelLayout, StructField, StructLayout

    class _Entry:
        def __init__(self, type_code: int):
            self.type_code = type_code

    class _Container:
        def __getitem__(self, index: int) -> _Entry:
            if index >= 3:
                raise IndexError
            return _Entry(index)

    layout = KernelLayout(
        structs={
            "struct rt_object_information": StructLayout(
                "struct rt_object_information",
                fields={"type": StructField("type", ("type",))},
            )
        }
    )
    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda name: _Container() if name == "_object_container" else None,
    )
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda entry, _layout, field: entry.type_code if field == "type" else None,
    )

    found = navigation.get_object_information(2, layout)

    assert found is not None
    assert found.type_code == 2


def test_get_tick_reads_the_up_tick_symbol(monkeypatch):
    """Uniprocessor ``rt_tick`` is a static global, not ``rt_tick_get()``."""
    monkeypatch.setattr(
        navigation, "lookup_symbol", lambda name: 42 if name == "rt_tick" else None
    )

    assert navigation.get_tick() == 42


def test_get_tick_reads_cpu0_when_rt_tick_is_absent(monkeypatch):
    """SMP builds expose the tick on ``_cpus[0].tick``."""
    monkeypatch.setattr(navigation, "gdb", _FakeGdb)
    table = _FakeValue(_FakeGdb.TYPE_CODE_ARRAY, [_Cpu(tick=77)])
    monkeypatch.setattr(
        navigation, "lookup_symbol", lambda name: table if name == "_cpus" else None
    )

    assert navigation.get_tick() == 77


def test_selected_cpu_id_prefers_halted_mpidr(monkeypatch):
    """Cortex-A affinity is the Aff0 field of the halted ``mpidr``."""
    monkeypatch.setattr(navigation, "gdb", _CpuGdb(frame=_Frame({"mpidr": 0x80000001})))

    assert navigation._selected_cpu_id() == 1


def test_selected_cpu_id_uses_mhartid(monkeypatch):
    """RISC-V current hart is the halted ``mhartid`` register."""
    monkeypatch.setattr(navigation, "gdb", _CpuGdb(frame=_Frame({"mhartid": 3})))

    assert navigation._selected_cpu_id() == 3


def test_selected_cpu_id_falls_back_to_gdb_thread(monkeypatch):
    """J-Link-style thread views map GDB thread numbers onto CPU indices."""
    monkeypatch.setattr(navigation, "gdb", _CpuGdb(thread=_Thread(2)))

    assert navigation._selected_cpu_id() == 1


def test_get_current_thread_uses_the_global_symbol(monkeypatch):
    """UP kernels export ``rt_current_thread`` as a global pointer."""
    current = object()
    monkeypatch.setattr(
        navigation,
        "lookup_symbol",
        lambda name: _Ptr(0x2000, current) if name == "rt_current_thread" else None,
    )

    assert navigation.get_current_thread() is current


def test_get_current_thread_reads_selected_cpu_from_table(monkeypatch):
    """SMP current thread is ``_cpus[cpu_id].current_thread``, not a helper call."""
    current = object()
    table = _FakeValue(
        _FakeGdb.TYPE_CODE_ARRAY, [None, _Cpu(current_thread=_Ptr(0x10, current))]
    )
    monkeypatch.setattr(navigation, "gdb", _FakeGdb)
    monkeypatch.setattr(navigation, "_selected_cpu_id", lambda: 1)

    def lookup(name: str):
        if name == "_cpus":
            return table
        return None

    monkeypatch.setattr(navigation, "lookup_symbol", lookup)

    assert navigation.get_current_thread() is current


def test_iter_timers_uses_resolved_list_heads(monkeypatch):
    """Active timer walks look up the array symbol, then index in Python."""
    from gdr.layout import KernelLayout, ListHook

    hook = ListHook(
        head_symbol="_timer_list",
        node_path=("row", 0),
        container_type="struct rt_timer",
        next_path=("next",),
        head_index=0,
    )
    layout = KernelLayout(list_hooks={"timer_list": hook})
    head = object()
    timer = type("Timer", (), {"address": 0x100})()
    monkeypatch.setattr(
        navigation, "resolve_list_head", lambda item: head if item is hook else None
    )
    monkeypatch.setattr(
        navigation,
        "iter_list",
        lambda head_value, _hook: iter([timer]) if head_value is head else iter(()),
    )
    monkeypatch.setattr(navigation, "iter_objects", lambda *_args: iter(()))

    assert list(navigation.iter_timers(layout)) == [timer]


def test_get_heap_used_reads_used_mem(monkeypatch):
    """small_mem / slab usage comes from the static ``used_mem`` symbol."""
    from gdr.layout import KernelLayout

    monkeypatch.setattr(
        navigation, "lookup_symbol", lambda name: 4096 if name == "used_mem" else None
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    assert navigation.get_heap_used("small_mem", KernelLayout()) == 4096


def test_get_heap_used_reads_memheap_pool_fields(monkeypatch):
    """memheap-as-heap usage is ``pool_size - available_size`` on ``_heap``."""
    from gdr.layout import KernelLayout, StructField, StructLayout

    heap = object()
    layout = KernelLayout(
        structs={
            "struct rt_memheap": StructLayout(
                "struct rt_memheap",
                fields={
                    "pool_size": StructField("pool_size", ("pool_size",)),
                    "available_size": StructField(
                        "available_size", ("available_size",)
                    ),
                },
            )
        }
    )
    monkeypatch.setattr(
        navigation, "lookup_symbol", lambda name: heap if name == "_heap" else None
    )
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda value, _layout, field: (
            {"pool_size": 1024, "available_size": 24}[field] if value is heap else None
        ),
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    assert navigation.get_heap_used("memheap", layout) == 1000


def test_get_heap_used_sums_rt_memory_objects(monkeypatch):
    """4.1 ``struct rt_memory`` heaps contribute their ``used`` fields."""
    from gdr.layout import KernelLayout, StructField, StructLayout

    values = [object(), object()]
    used_by_id = {id(values[0]): 10, id(values[1]): 15}
    layout = KernelLayout(
        object_codes={"memory": 12},
        structs={
            "struct rt_memory": StructLayout(
                "struct rt_memory",
                fields={"used": StructField("used", ("used",))},
            )
        },
    )
    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)
    monkeypatch.setattr(navigation, "iter_objects", lambda _code, _layout: iter(values))
    monkeypatch.setattr(
        navigation,
        "read_field",
        lambda value, _layout, _field: used_by_id[id(value)],
    )
    monkeypatch.setattr(navigation, "read_int", lambda value: value)

    assert navigation.get_heap_used("none", layout) == 25


def test_get_heap_used_is_unavailable_without_target_state(monkeypatch):
    """Missing heap symbols do not fall back to calling ``rt_memory_info``."""
    from gdr.layout import KernelLayout

    monkeypatch.setattr(navigation, "lookup_symbol", lambda _name: None)

    assert navigation.get_heap_used("small_mem", KernelLayout()) is None
