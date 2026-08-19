"""Unit tests for RT-Thread layout metadata."""

from __future__ import annotations

import rtthread.layout as layout_module
from rtthread.layout import (
    RT_OBJECT_CLASS_THREAD,
    RtConfig,
    ThreadState,
    build_layouts,
)


class _FakeGdb:
    """GDB constants needed by the heap-type probe."""

    TYPE_CODE_TYPEDEF = 1
    TYPE_CODE_PTR = 2
    TYPE_CODE_STRUCT = 3

    class error(Exception):
        pass


class _Handle:
    """Minimal ``system_heap`` handle stand-in with a GDB-like type."""

    def __init__(self, type_obj):
        self.type = type_obj


class _TypedefType:
    """``TYPE_CODE_TYPEDEF`` wrapper; the alias name lives here, not on the PTR."""

    def __init__(self, name: str, stripped):
        self.code = _FakeGdb.TYPE_CODE_TYPEDEF
        self.name = name
        self.tag = None
        self._stripped = stripped

    def strip_typedefs(self):
        return self._stripped


class _PointerType:
    """Nameless pointer after ``strip_typedefs()``, matching real GDB."""

    def __init__(self, target):
        self.code = _FakeGdb.TYPE_CODE_PTR
        self.name = None
        self.tag = None
        self._target = target

    def target(self):
        return self._target

    def strip_typedefs(self):
        return self


class _StructType:
    def __init__(self, name: str | None, tag: str | None = None):
        self.name = name
        self.tag = name if tag is None else tag
        self.code = _FakeGdb.TYPE_CODE_STRUCT

    def strip_typedefs(self):
        return self

    def fields(self):
        return ()


def _typedef_ptr_handle(typedef_name: str, target_name: str = "rt_memory") -> _Handle:
    """Build a 4.1 ``rt_smem_t`` / ``rt_slab_t`` system_heap handle."""
    return _Handle(_TypedefType(typedef_name, _PointerType(_StructType(target_name))))


def test_layout_retains_probed_stack_direction():
    """The adapter passes its target stack direction into the generic layout."""
    assert build_layouts(RtConfig(stack_grows_up=True)).stack_grows_up is True


def test_thread_state_masks_flags_and_handles_unknown_values():
    """RT-Thread state decoding belongs to the RT-Thread adapter."""
    assert ThreadState.from_raw(0x83) is ThreadState.RUNNING
    assert ThreadState.from_raw(0x07) is ThreadState.UNKNOWN


def test_layout_supplies_display_and_intrusive_list_metadata():
    """Core printers and traversal consume metadata rather than RT-Thread names."""
    layout = build_layouts(RtConfig(using_mutex=True))

    assert layout.structs["struct rt_thread"].display_name == "Thread"
    assert layout.structs["struct rt_mutex"].fields["owner"].pointee_string_path == (
        "name",
    )
    assert layout.object_types[RT_OBJECT_CLASS_THREAD].next_path == ("next",)


def test_legacy_31_profile_uses_pre_null_object_codes_and_flags_field():
    """3.1.0-3.1.2 predate the ``Null = 0`` object enum entry."""
    layout = build_layouts(RtConfig(using_semaphore=True), (3, 1, 0))

    assert layout.object_codes["thread"] == 0
    assert layout.object_codes["semaphore"] == 1
    assert layout.structs["struct rt_thread"].fields["flag"].path == ("flags",)
    assert layout.structs["struct rt_thread"].fields["stat"].path == ("stat",)
    assert "reserved" not in layout.structs["struct rt_semaphore"].fields
    from rtthread.layout import resolve_object_type_code

    assert resolve_object_type_code("semaphore", layout) == 1
    assert resolve_object_type_code("SEMAPHORE", layout) == 1


def test_resolve_object_type_code_accepts_display_and_semantic_names():
    """Type-name lookup is case-insensitive for command and convenience use."""
    from rtthread.layout import resolve_object_type_code

    layout = build_layouts(RtConfig(using_semaphore=True), (4, 0, 5))

    assert resolve_object_type_code("SEMAPHORE", layout) == 2
    assert resolve_object_type_code("semaphore", layout) == 2
    assert resolve_object_type_code("MSGQUEUE", layout) == 6
    assert resolve_object_type_code("missing", layout) is None


def test_modern_31_profile_uses_null_shifted_object_codes():
    """3.1.3 retains its enum migration and added semaphore field."""
    layout = build_layouts(
        RtConfig(using_semaphore=True, using_soft_timer=True), (3, 1, 3)
    )

    assert layout.object_codes["thread"] == 1
    assert layout.object_codes["semaphore"] == 2
    assert layout.object_types[2].name == "semaphore"
    assert "reserved" in layout.structs["struct rt_semaphore"].fields
    assert layout.list_hooks["timer_list"].head_symbol == "rt_timer_list"
    assert layout.list_hooks["timer_list"].head_index == 0
    assert layout.list_hooks["soft_timer_list"].head_symbol == "rt_soft_timer_list"
    assert layout.list_hooks["soft_timer_list"].head_index == 0


def test_4x_soft_timer_hook_requires_its_own_config_probe():
    """A device-enabled target alone does not prove soft timers are compiled."""
    no_soft = build_layouts(RtConfig(using_device=True), (4, 0, 5))
    soft = build_layouts(RtConfig(using_soft_timer=True), (4, 0, 5))

    assert "soft_timer_list" not in no_soft.list_hooks
    assert no_soft.list_hooks["timer_list"].head_symbol == "_timer_list"
    assert no_soft.list_hooks["timer_list"].head_index == 0
    assert soft.list_hooks["soft_timer_list"].head_symbol == "_soft_timer_list"
    assert soft.list_hooks["soft_timer_list"].head_index == 0


def test_messagequeue_layout_describes_receiver_suspend_list():
    """MQ always describes the receiver suspend list on the current version."""
    layout = build_layouts(RtConfig(using_messagequeue=True), (4, 0, 5))
    mq_fields = layout.structs["struct rt_messagequeue"].fields

    assert "suspend_thread" in mq_fields
    assert mq_fields["suspend_thread"].path == ("parent", "suspend_thread")


def test_mempool_layout_describes_suspend_thread_list():
    """Waiter counts come from the list, never the removed count field."""
    layout = build_layouts(RtConfig(using_mempool=True), (4, 1, 1))
    mempool = layout.structs["struct rt_mempool"].fields

    assert mempool["suspend_thread"].path == ("suspend_thread",)
    assert mempool["suspend_thread"].kind == "list"
    # The old cached count field is deliberately not part of the layout.
    assert "suspend_thread_count" not in mempool


def test_mempool_layout_describes_block_list_for_detail():
    """block_list exists for detail diagnostics, not the default table."""
    layout = build_layouts(RtConfig(using_mempool=True), (4, 1, 1))
    mempool = layout.structs["struct rt_mempool"].fields

    assert mempool["block_list"].path == ("block_list",)
    assert mempool["block_list"].kind == "ptr"


def test_timer_layout_describes_parameter_field():
    """Timer detail exposes the callback parameter pointer."""
    layout = build_layouts(RtConfig(), (4, 1, 1))
    timer = layout.structs["struct rt_timer"].fields

    assert timer["parameter"].path == ("parameter",)
    assert timer["parameter"].kind == "ptr"


def test_system_heap_algorithm_returns_none_without_a_handle():
    """No ``system_heap`` symbol means the 4.0 globals decide."""
    assert layout_module._system_heap_algorithm(None, lambda _name: False) is None


def test_system_heap_algorithm_classifies_memheap_value(monkeypatch):
    """A plain ``struct rt_memheap`` system_heap handle is memheap."""
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)
    handle = _Handle(_StructType("rt_memheap"))

    assert (
        layout_module._system_heap_algorithm(handle, lambda _name: False) == "memheap"
    )


def test_system_heap_algorithm_classifies_memheap_from_type_tag(monkeypatch):
    """Some GDB builds expose the struct identity on ``tag`` only."""
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)
    handle = _Handle(_StructType(None, tag="rt_memheap"))

    assert (
        layout_module._system_heap_algorithm(handle, lambda _name: False) == "memheap"
    )


def test_system_heap_algorithm_classifies_slab_typedef(monkeypatch):
    """A ``rt_slab_t`` typedef wins over the shared ``struct rt_memory *`` shape."""
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)
    handle = _typedef_ptr_handle("rt_slab_t")

    assert layout_module._system_heap_algorithm(handle, lambda _name: False) == "slab"


def test_system_heap_algorithm_defaults_to_small_mem_for_memory_pointer(monkeypatch):
    """A ``rt_smem_t`` typedef to ``struct rt_memory *`` classifies as small_mem."""
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)
    handle = _typedef_ptr_handle("rt_smem_t")

    assert (
        layout_module._system_heap_algorithm(handle, lambda _name: False) == "small_mem"
    )


def test_detect_config_uses_heap_end_for_40_small_mem(monkeypatch):
    """4.0 kernels expose the system heap as static globals, not a handle."""
    import gdr.gdb_bridge as bridge

    present = {"heap_end", "heap_ptr", "used_mem"}
    monkeypatch.setattr(bridge, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(
        bridge, "lookup_symbol", lambda name: object() if name in present else None
    )
    monkeypatch.setattr(bridge, "lookup_type", lambda _name: None)
    monkeypatch.setattr(bridge, "macro_defined", lambda _name: False)
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)

    cfg = layout_module.detect_config()

    assert cfg.heap_type == "small_mem"


def test_detect_config_prefers_system_heap_type_over_init_symbols(monkeypatch):
    """A 4.1 ``system_heap`` handle wins even when several allocators compile.

    The QEMU 4.1 fixture compiles ``rt_smem_init``, ``rt_slab_init`` and
    ``rt_memheap_init`` together, so the init symbols alone cannot select the
    active ``*_AS_HEAP``; the ``system_heap`` handle must decide.
    """
    import gdr.gdb_bridge as bridge

    present = {"rt_smem_init", "rt_slab_init", "rt_memheap_init"}
    handle = _typedef_ptr_handle("rt_smem_t")
    monkeypatch.setattr(bridge, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(
        bridge, "lookup_symbol", lambda name: handle if name == "system_heap" else None
    )
    monkeypatch.setattr(bridge, "lookup_type", lambda _name: None)
    monkeypatch.setattr(bridge, "macro_defined", lambda _name: False)
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)

    cfg = layout_module.detect_config()

    assert cfg.heap_type == "small_mem"


def test_detect_config_classifies_slab_typedef_despite_smem_init(monkeypatch):
    """``rt_slab_t system_heap`` wins even when ``rt_smem_init`` is also linked."""
    import gdr.gdb_bridge as bridge

    present = {"rt_smem_init", "rt_slab_init", "rt_memheap_init"}
    handle = _typedef_ptr_handle("rt_slab_t")
    monkeypatch.setattr(bridge, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(
        bridge, "lookup_symbol", lambda name: handle if name == "system_heap" else None
    )
    monkeypatch.setattr(bridge, "lookup_type", lambda _name: None)
    monkeypatch.setattr(bridge, "macro_defined", lambda _name: False)
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)

    cfg = layout_module.detect_config()

    assert cfg.heap_type == "slab"


def test_detect_config_probes_memtrace_from_block_header(monkeypatch):
    """MEMTRACE is proven by a block-header owner field, not a FINSH symbol."""
    import gdr.gdb_bridge as bridge

    present = {"heap_end"}
    monkeypatch.setattr(bridge, "symbol_exists", lambda name: name in present)
    monkeypatch.setattr(
        bridge, "lookup_symbol", lambda name: object() if name in present else None
    )

    class _Field:
        def __init__(self, name: str):
            self.name = name

    def fake_lookup_type(name: str):
        if name == "struct heap_mem":
            return type(
                "_T",
                (),
                {"fields": lambda _self=None: [_Field("magic"), _Field("thread")]},
            )()
        return None

    monkeypatch.setattr(bridge, "lookup_type", fake_lookup_type)
    monkeypatch.setattr(bridge, "macro_defined", lambda _name: False)
    monkeypatch.setattr(layout_module, "gdb", _FakeGdb)

    cfg = layout_module.detect_config()

    assert cfg.using_memtrace is True


def test_build_layouts_adds_heap_block_headers_for_the_probed_algorithm():
    """Only the active system-heap ABI is described; MEMTRACE fields stay optional."""
    small_40 = build_layouts(RtConfig(heap_type="small_mem", using_memtrace=True))
    assert "struct heap_mem" in small_40.structs
    assert "struct rt_small_mem" not in small_40.structs
    assert "thread" in small_40.structs["struct heap_mem"].fields

    small_41 = build_layouts(
        RtConfig(heap_type="small_mem", using_memory_object=True, using_memtrace=True)
    )
    assert "struct rt_small_mem" in small_41.structs
    assert "struct rt_small_mem_item" in small_41.structs
    assert "struct heap_mem" not in small_41.structs
    assert "thread" in small_41.structs["struct rt_small_mem_item"].fields

    memheap = build_layouts(RtConfig(heap_type="memheap", using_memtrace=True))
    assert "struct rt_memheap_item" in memheap.structs
    assert "owner_thread_name" in memheap.structs["struct rt_memheap_item"].fields
    assert "struct heap_mem" not in memheap.structs

    slab = build_layouts(RtConfig(heap_type="slab", using_memory_object=True))
    assert "struct rt_slab" in slab.structs
    assert "struct heap_mem" not in slab.structs

    unused = build_layouts(RtConfig())
    assert "struct heap_mem" not in unused.structs
    assert "struct rt_memheap_item" not in unused.structs
    assert "struct rt_slab" not in unused.structs
