"""RT-Thread v4.0.x kernel layout descriptions.

This is the **single place** that knows RT-Thread struct layouts.  When an
RT-Thread kernel struct changes (new field, renamed member, shifted offset),
this file — and its QEMU smoke test — must be reviewed together.

Design notes
------------
* **Path-based access, not hardcoded offsets.**  GDB resolves field paths via
  DWARF, so we don't need to track byte offsets that vary with config.
* **Config-conditional fields via factory functions.**  ``build_thread_layout``
  inspects ``RtConfig`` and adds SMP fields only when ``config.smp`` is True.
  This handles the real source of struct variation (config, not version).
* **Flat vs nested inheritance.**  ``rt_thread`` flattens ``rt_object`` fields
  directly (depth 0), while ``rt_timer`` embeds via ``parent`` (depth 1) and
  ``rt_semaphore`` embeds via ``parent.parent`` (depth 2, through
  ``rt_ipc_object``).  The ``_object_fields`` helper generates the right paths.
"""

from __future__ import annotations

from dataclasses import dataclass

from gdr.layout import (
    KernelLayout,
    ListHook,
    ObjectTypeInfo,
    StructField,
    StructLayout,
)

# ---------------------------------------------------------------------------
# Constants — RT-Thread object type codes (rtdef.h enum rt_object_class_type)
# ---------------------------------------------------------------------------

RT_OBJECT_CLASS_THREAD = 0x01
RT_OBJECT_CLASS_SEMAPHORE = 0x02
RT_OBJECT_CLASS_MUTEX = 0x03
RT_OBJECT_CLASS_EVENT = 0x04
RT_OBJECT_CLASS_MAILBOX = 0x05
RT_OBJECT_CLASS_MESSAGEQUEUE = 0x06
RT_OBJECT_CLASS_MEMHEAP = 0x07
RT_OBJECT_CLASS_MEMPOOL = 0x08
RT_OBJECT_CLASS_DEVICE = 0x09
RT_OBJECT_CLASS_TIMER = 0x0A
RT_OBJECT_CLASS_MEMORY = 0x0C

# Timer flag bits (rtdef.h)
RT_TIMER_FLAG_ACTIVATED = 0x1
RT_TIMER_FLAG_PERIODIC = 0x2
RT_TIMER_FLAG_SOFT_TIMER = 0x4

# Thread stat mask (rtdef.h)
RT_THREAD_STAT_MASK = 0x07

# Timer skip-list level (rtdef.h default is 1)
RT_TIMER_SKIP_LIST_LEVEL = 1

# Object type code → display name (matches rt_object_class_type enum order).
# Reason: re-using type codes as keys keeps a single source of truth; the
# pretty-printer renders ``type=THREAD`` instead of ``type=1``.
OBJECT_TYPE_NAMES: dict[int, str] = {
    RT_OBJECT_CLASS_THREAD: "THREAD",
    RT_OBJECT_CLASS_SEMAPHORE: "SEMAPHORE",
    RT_OBJECT_CLASS_MUTEX: "MUTEX",
    RT_OBJECT_CLASS_EVENT: "EVENT",
    RT_OBJECT_CLASS_MAILBOX: "MAILBOX",
    RT_OBJECT_CLASS_MESSAGEQUEUE: "MSGQUEUE",
    RT_OBJECT_CLASS_MEMHEAP: "MEMHEAP",
    RT_OBJECT_CLASS_MEMPOOL: "MEMPOOL",
    RT_OBJECT_CLASS_DEVICE: "DEVICE",
    RT_OBJECT_CLASS_TIMER: "TIMER",
    RT_OBJECT_CLASS_MEMORY: "MEMORY",
}

# Thread stat → display name (low 3 bits of rt_thread.stat).  Matches
# ThreadState in gdr.abstractions; duplicated here only to keep rtthread/
# self-contained for the printer's enum_map (abstractions defines the IntEnum
# with the same values).
THREAD_STAT_NAMES: dict[int, str] = {
    0x00: "INIT",
    0x01: "READY",
    0x02: "SUSPEND",
    0x03: "RUNNING",
    0x04: "CLOSE",
}

# Timer flag bits → display name (for ``flag`` field on rt_timer / rt_object).
TIMER_FLAG_NAMES: dict[int, str] = {
    RT_TIMER_FLAG_ACTIVATED: "ACTIVE",
    RT_TIMER_FLAG_PERIODIC: "PERIODIC",
    RT_TIMER_FLAG_SOFT_TIMER: "SOFT",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RtConfig:
    """Probed RT-Thread kernel configuration.

    Attributes mirror the ``RT_USING_*`` macros that conditionally compile
    kernel components.  All fields are probed at runtime by ``detect_config``;
    users never specify them manually.
    """

    smp: bool = False
    using_module: bool = False
    using_semaphore: bool = False
    using_mutex: bool = False
    using_event: bool = False
    using_mailbox: bool = False
    using_messagequeue: bool = False
    using_memheap: bool = False
    using_mempool: bool = False
    using_device: bool = False
    using_signals: bool = False
    using_lwp: bool = False
    using_cpu_usage: bool = False
    using_memory_object: bool = False
    thread_has_init_priority: bool = True
    thread_has_pthread_data: bool = False
    heap_type: str = "none"  # "small_mem", "slab", "memheap", "none"


def detect_config() -> RtConfig:
    """Probe the target's RT-Thread kernel configuration by symbol presence.

    This is intentionally lightweight: each check is a single symbol lookup.
    Probing is reliable because it only tests *existence*, not semantics.
    When a probe is inconclusive, the field defaults to ``False`` and the
    corresponding layout fields are simply omitted (safe degradation).

    Returns:
        Populated ``RtConfig``.

    Raises:
        RuntimeError: if not running inside GDB.
    """
    from gdr.gdb_bridge import lookup_type, symbol_exists

    cfg = RtConfig()

    cfg.smp = symbol_exists("rt_cpu_index")
    cfg.using_module = symbol_exists("rt_dlmodule_init")
    cfg.using_semaphore = symbol_exists("rt_sem_init")
    cfg.using_mutex = symbol_exists("rt_mutex_init")
    cfg.using_event = symbol_exists("rt_event_init")
    cfg.using_mailbox = symbol_exists("rt_mb_init")
    cfg.using_messagequeue = symbol_exists("rt_mq_init")
    cfg.using_memheap = symbol_exists("rt_memheap_init")
    cfg.using_mempool = symbol_exists("rt_mp_init")
    cfg.using_device = symbol_exists("rt_device_register")
    cfg.using_signals = symbol_exists("rt_signal_init")
    cfg.using_lwp = symbol_exists("lwp_pid_find")

    cfg.using_memory_object = lookup_type("struct rt_memory") is not None

    # Reason: heap_type is probed by internal symbols first for 4.0.x, then by
    # allocator entry points for 4.1.x where heap implementations are wrapped
    # by struct rt_memory objects instead of exposed static globals.
    if cfg.using_memory_object and symbol_exists("rt_smem_init"):
        cfg.heap_type = "small_mem"
    elif symbol_exists("memusage"):
        cfg.heap_type = "slab"
    elif symbol_exists("heap_end"):
        cfg.heap_type = "small_mem"
    elif cfg.using_memheap and symbol_exists("_heap"):
        cfg.heap_type = "memheap"
    else:
        cfg.heap_type = "none"

    # CPU usage tracking adds a field to rt_thread; detect by type introspection
    rt_thread_type = lookup_type("struct rt_thread")
    if rt_thread_type is not None:
        thread_fields = {f.name for f in rt_thread_type.fields()}
        cfg.thread_has_init_priority = "init_priority" in thread_fields
        cfg.thread_has_pthread_data = "pthread_data" in thread_fields
        cfg.using_cpu_usage = any(f_name == "duration_tick" for f_name in thread_fields)

    return cfg


# ---------------------------------------------------------------------------
# Field-path helpers
# ---------------------------------------------------------------------------


def _object_fields(depth: int) -> dict[str, StructField]:
    """Generate common ``rt_object`` fields at the given parent nesting depth.

    Args:
        depth: Number of ``parent`` hops to reach ``rt_object``.
            0 = flat (``rt_thread``), 1 = one ``parent`` (``rt_timer``),
            2 = two ``parent`` (IPC objects via ``rt_ipc_object``).

    Returns:
        Dict with ``name``, ``type``, ``flag``, ``list`` fields.
    """
    p = ("parent",) * depth
    return {
        "name": StructField("name", (*p, "name"), kind="string", summary=True),
        "type": StructField(
            "type", (*p, "type"), kind="enum", enum_map=OBJECT_TYPE_NAMES
        ),
        "flag": StructField("flag", (*p, "flag"), kind="flags"),
        "list": StructField("list", (*p, "list"), kind="list"),
    }


def _ipc_fields() -> dict[str, StructField]:
    """Fields for structs inheriting from ``rt_ipc_object`` (depth 2)."""
    fields = _object_fields(2)
    fields["suspend_thread"] = StructField(
        "suspend_thread", ("parent", "suspend_thread"), kind="list"
    )
    return fields


# ---------------------------------------------------------------------------
# Struct layout builders
# ---------------------------------------------------------------------------


def build_thread_layout(cfg: RtConfig) -> StructLayout:
    """Build ``rt_thread`` layout (COUPLED: rtdef.h struct rt_thread).

    ``rt_thread`` flattens ``rt_object`` fields directly — no ``parent``
    embedding.  SMP adds ``bind_cpu`` / ``oncpu`` / lock-nest counters.
    """
    sl = StructLayout("struct rt_thread")
    f = sl.fields

    # Flat rt_object fields (depth 0)
    f.update(_object_fields(0))

    # Thread scheduling list (separate from object list)
    f["tlist"] = StructField("tlist", ("tlist",), kind="list")

    # Stack and entry
    f["sp"] = StructField("sp", ("sp",), kind="ptr")
    f["entry"] = StructField("entry", ("entry",), kind="ptr")
    f["parameter"] = StructField("parameter", ("parameter",), kind="ptr")
    f["stack_addr"] = StructField("stack_addr", ("stack_addr",), kind="ptr")
    f["stack_size"] = StructField("stack_size", ("stack_size",))

    # Error and state
    f["error"] = StructField("error", ("error",))
    f["stat"] = StructField(
        "stat", ("stat",), kind="enum", summary=True, enum_map=THREAD_STAT_NAMES
    )

    # SMP-conditional fields
    if cfg.smp:
        f["bind_cpu"] = StructField("bind_cpu", ("bind_cpu",))
        f["oncpu"] = StructField("oncpu", ("oncpu",))
        f["scheduler_lock_nest"] = StructField(
            "scheduler_lock_nest", ("scheduler_lock_nest",)
        )
        f["cpus_lock_nest"] = StructField("cpus_lock_nest", ("cpus_lock_nest",))
        f["critical_lock_nest"] = StructField(
            "critical_lock_nest", ("critical_lock_nest",)
        )

    # Priority
    f["current_priority"] = StructField(
        "current_priority", ("current_priority",), summary=True
    )
    if cfg.thread_has_init_priority:
        f["init_priority"] = StructField("init_priority", ("init_priority",))
    f["number_mask"] = StructField("number_mask", ("number_mask",))

    # Ticks
    f["init_tick"] = StructField("init_tick", ("init_tick",))
    f["remaining_tick"] = StructField("remaining_tick", ("remaining_tick",))

    # Embedded timer and cleanup
    f["thread_timer"] = StructField("thread_timer", ("thread_timer",))
    f["cleanup"] = StructField("cleanup", ("cleanup",), kind="ptr")
    f["user_data"] = StructField("user_data", ("user_data",), kind="ptr")

    # Optional config-conditional fields
    if cfg.using_event:
        f["event_set"] = StructField("event_set", ("event_set",), optional=True)
        f["event_info"] = StructField("event_info", ("event_info",), optional=True)
    if cfg.using_cpu_usage:
        f["duration_tick"] = StructField(
            "duration_tick", ("duration_tick",), optional=True
        )
    if cfg.thread_has_pthread_data:
        f["pthread_data"] = StructField(
            "pthread_data", ("pthread_data",), kind="ptr", optional=True
        )

    return sl


def build_timer_layout() -> StructLayout:
    """Build ``rt_timer`` layout (COUPLED: rtdef.h struct rt_timer)."""
    sl = StructLayout("struct rt_timer")
    sl.fields.update(_object_fields(1))  # parent = rt_object
    # Reason: ``flag`` is shared by all rt_object subclasses, but only the
    # timer interpretation is meaningful here; override the field with a
    # timer-specific bit map so the printer renders ``flag=ACTIVE|PERIODIC``.
    sl.fields["flag"] = StructField(
        "flag",
        ("parent", "flag"),
        kind="flags",
        summary=True,
        enum_map=TIMER_FLAG_NAMES,
    )
    sl.fields["row"] = StructField(
        "row", ("row", 0), kind="list"
    )  # row[0] for skip-list level 1
    sl.fields["timeout_func"] = StructField(
        "timeout_func", ("timeout_func",), kind="ptr"
    )
    sl.fields["parameter"] = StructField("parameter", ("parameter",), kind="ptr")
    sl.fields["init_tick"] = StructField("init_tick", ("init_tick",), summary=True)
    sl.fields["timeout_tick"] = StructField(
        "timeout_tick", ("timeout_tick",), summary=True
    )
    return sl


def build_semaphore_layout() -> StructLayout:
    """Build ``rt_semaphore`` layout (COUPLED: rtdef.h struct rt_semaphore)."""
    sl = StructLayout("struct rt_semaphore")
    sl.fields.update(_ipc_fields())  # parent.parent = rt_object
    sl.fields["value"] = StructField("value", ("value",), summary=True)
    sl.fields["reserved"] = StructField("reserved", ("reserved",))
    return sl


def build_mutex_layout() -> StructLayout:
    """Build ``rt_mutex`` layout (COUPLED: rtdef.h struct rt_mutex)."""
    sl = StructLayout("struct rt_mutex")
    sl.fields.update(_ipc_fields())
    sl.fields["value"] = StructField("value", ("value",), summary=True)
    sl.fields["original_priority"] = StructField(
        "original_priority", ("original_priority",)
    )
    sl.fields["hold"] = StructField("hold", ("hold",), summary=True)
    sl.fields["owner"] = StructField("owner", ("owner",), kind="ptr", summary=True)
    return sl


def build_event_layout() -> StructLayout:
    """Build ``rt_event`` layout (COUPLED: rtdef.h struct rt_event)."""
    sl = StructLayout("struct rt_event")
    sl.fields.update(_ipc_fields())
    sl.fields["set"] = StructField("set", ("set",), summary=True)
    return sl


def build_mailbox_layout() -> StructLayout:
    """Build ``rt_mailbox`` layout (COUPLED: rtdef.h struct rt_mailbox)."""
    sl = StructLayout("struct rt_mailbox")
    sl.fields.update(_ipc_fields())
    sl.fields["msg_pool"] = StructField("msg_pool", ("msg_pool",), kind="ptr")
    sl.fields["size"] = StructField("size", ("size",), summary=True)
    sl.fields["entry"] = StructField("entry", ("entry",), summary=True)
    sl.fields["in_offset"] = StructField("in_offset", ("in_offset",))
    sl.fields["out_offset"] = StructField("out_offset", ("out_offset",))
    sl.fields["suspend_sender_thread"] = StructField(
        "suspend_sender_thread", ("suspend_sender_thread",), kind="list"
    )
    return sl


def build_messagequeue_layout() -> StructLayout:
    """Build ``rt_messagequeue`` layout (COUPLED: rtdef.h struct rt_messagequeue)."""
    sl = StructLayout("struct rt_messagequeue")
    sl.fields.update(_ipc_fields())
    sl.fields["msg_pool"] = StructField("msg_pool", ("msg_pool",), kind="ptr")
    sl.fields["msg_size"] = StructField("msg_size", ("msg_size",))
    sl.fields["max_msgs"] = StructField("max_msgs", ("max_msgs",), summary=True)
    sl.fields["entry"] = StructField("entry", ("entry",), summary=True)
    sl.fields["msg_queue_head"] = StructField(
        "msg_queue_head", ("msg_queue_head",), kind="ptr"
    )
    sl.fields["msg_queue_tail"] = StructField(
        "msg_queue_tail", ("msg_queue_tail",), kind="ptr"
    )
    sl.fields["msg_queue_free"] = StructField(
        "msg_queue_free", ("msg_queue_free",), kind="ptr"
    )
    sl.fields["suspend_sender_thread"] = StructField(
        "suspend_sender_thread", ("suspend_sender_thread",), kind="list"
    )
    return sl


def build_memheap_layout() -> StructLayout:
    """Build ``rt_memheap`` layout (COUPLED: rtdef.h struct rt_memheap)."""
    sl = StructLayout("struct rt_memheap")
    sl.fields.update(_object_fields(1))  # parent = rt_object
    sl.fields["start_addr"] = StructField("start_addr", ("start_addr",), kind="ptr")
    sl.fields["pool_size"] = StructField("pool_size", ("pool_size",), summary=True)
    sl.fields["available_size"] = StructField(
        "available_size", ("available_size",), summary=True
    )
    sl.fields["max_used_size"] = StructField("max_used_size", ("max_used_size",))
    return sl


def build_mempool_layout() -> StructLayout:
    """Build ``rt_mempool`` layout (COUPLED: rtdef.h struct rt_mempool)."""
    sl = StructLayout("struct rt_mempool")
    sl.fields.update(_object_fields(1))  # parent = rt_object
    sl.fields["start_address"] = StructField(
        "start_address", ("start_address",), kind="ptr"
    )
    sl.fields["size"] = StructField("size", ("size",))
    sl.fields["block_size"] = StructField("block_size", ("block_size",))
    sl.fields["block_total_count"] = StructField(
        "block_total_count", ("block_total_count",), summary=True
    )
    sl.fields["block_free_count"] = StructField(
        "block_free_count", ("block_free_count",), summary=True
    )
    return sl


def build_memory_layout() -> StructLayout:
    """Build ``rt_memory`` layout (RT-Thread 4.1.x heap object)."""
    sl = StructLayout("struct rt_memory")
    sl.fields.update(_object_fields(1))  # parent = rt_object
    sl.fields["algorithm"] = StructField("algorithm", ("algorithm",), kind="string")
    sl.fields["address"] = StructField("address", ("address",), kind="ptr")
    sl.fields["total"] = StructField("total", ("total",), summary=True)
    sl.fields["used"] = StructField("used", ("used",), summary=True)
    sl.fields["max"] = StructField("max", ("max",), summary=True)
    return sl


def build_object_information_layout() -> StructLayout:
    """Build ``rt_object_information`` layout (COUPLED: rtdef.h)."""
    sl = StructLayout("struct rt_object_information")
    sl.fields["type"] = StructField("type", ("type",), kind="enum")
    sl.fields["object_list"] = StructField("object_list", ("object_list",), kind="list")
    sl.fields["object_size"] = StructField("object_size", ("object_size",))
    return sl


# ---------------------------------------------------------------------------
# Object type registry and list hooks
# ---------------------------------------------------------------------------

# Type code -> (struct_name, list_path) for container_of when iterating
# the object container's object_list.  The list_path is the path from the
# container struct to the rt_list_t member that links it into the container.
_ALL_OBJECT_TYPES: list[ObjectTypeInfo] = [
    ObjectTypeInfo(RT_OBJECT_CLASS_THREAD, "struct rt_thread", ("list",)),
    ObjectTypeInfo(
        RT_OBJECT_CLASS_SEMAPHORE, "struct rt_semaphore", ("parent", "parent", "list")
    ),
    ObjectTypeInfo(
        RT_OBJECT_CLASS_MUTEX, "struct rt_mutex", ("parent", "parent", "list")
    ),
    ObjectTypeInfo(
        RT_OBJECT_CLASS_EVENT, "struct rt_event", ("parent", "parent", "list")
    ),
    ObjectTypeInfo(
        RT_OBJECT_CLASS_MAILBOX, "struct rt_mailbox", ("parent", "parent", "list")
    ),
    ObjectTypeInfo(
        RT_OBJECT_CLASS_MESSAGEQUEUE,
        "struct rt_messagequeue",
        ("parent", "parent", "list"),
    ),
    ObjectTypeInfo(RT_OBJECT_CLASS_MEMHEAP, "struct rt_memheap", ("parent", "list")),
    ObjectTypeInfo(RT_OBJECT_CLASS_MEMPOOL, "struct rt_mempool", ("parent", "list")),
    ObjectTypeInfo(RT_OBJECT_CLASS_DEVICE, "struct rt_device", ("parent", "list")),
    ObjectTypeInfo(RT_OBJECT_CLASS_TIMER, "struct rt_timer", ("parent", "list")),
    ObjectTypeInfo(RT_OBJECT_CLASS_MEMORY, "struct rt_memory", ("parent", "list")),
]


def _build_object_types(cfg: RtConfig) -> dict[int, ObjectTypeInfo]:
    """Filter the global type list by the probed config."""
    enabled_map = {
        RT_OBJECT_CLASS_SEMAPHORE: cfg.using_semaphore,
        RT_OBJECT_CLASS_MUTEX: cfg.using_mutex,
        RT_OBJECT_CLASS_EVENT: cfg.using_event,
        RT_OBJECT_CLASS_MAILBOX: cfg.using_mailbox,
        RT_OBJECT_CLASS_MESSAGEQUEUE: cfg.using_messagequeue,
        RT_OBJECT_CLASS_MEMHEAP: cfg.using_memheap,
        RT_OBJECT_CLASS_MEMPOOL: cfg.using_mempool,
        RT_OBJECT_CLASS_DEVICE: cfg.using_device,
        RT_OBJECT_CLASS_MEMORY: cfg.using_memory_object,
    }
    result = {}
    for info in _ALL_OBJECT_TYPES:
        info_copy = ObjectTypeInfo(
            info.type_code,
            info.struct_name,
            info.list_path,
            enabled=enabled_map.get(info.type_code, True),
        )
        result[info.type_code] = info_copy
    return result


def _build_list_hooks(cfg: RtConfig) -> dict[str, ListHook]:
    """Build list hooks for timer lists and priority table."""
    hooks: dict[str, ListHook] = {}

    # Hard timer list (timer.c: static rt_list_t _timer_list[1])
    hooks["timer_list"] = ListHook(
        head_expr=f"_timer_list[{RT_TIMER_SKIP_LIST_LEVEL - 1}]",
        node_path=("row", RT_TIMER_SKIP_LIST_LEVEL - 1),
        container_type="struct rt_timer",
    )

    # Soft timer list (only if soft timer is compiled in)
    if cfg.using_device:  # Reason: soft timer depends on timer thread
        hooks["soft_timer_list"] = ListHook(
            head_expr=f"_soft_timer_list[{RT_TIMER_SKIP_LIST_LEVEL - 1}]",
            node_path=("row", RT_TIMER_SKIP_LIST_LEVEL - 1),
            container_type="struct rt_timer",
        )

    return hooks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_layouts(cfg: RtConfig) -> KernelLayout:
    """Assemble a complete ``KernelLayout`` from the probed configuration.

    This is the main entry point for the RT-Thread adapter.  The resulting
    ``KernelLayout`` is passed to ``gdr.kernel`` navigation functions and
    ``gdr.printers`` for pretty-printer registration.

    Args:
        cfg: Probed ``RtConfig`` from ``detect_config``.

    Returns:
        ``KernelLayout`` with struct layouts, list hooks, and object types.
    """
    kl = KernelLayout()

    # Struct layouts
    kl.structs["struct rt_thread"] = build_thread_layout(cfg)
    kl.structs["struct rt_timer"] = build_timer_layout()
    kl.structs["struct rt_object_information"] = build_object_information_layout()

    if cfg.using_semaphore:
        kl.structs["struct rt_semaphore"] = build_semaphore_layout()
    if cfg.using_mutex:
        kl.structs["struct rt_mutex"] = build_mutex_layout()
    if cfg.using_event:
        kl.structs["struct rt_event"] = build_event_layout()
    if cfg.using_mailbox:
        kl.structs["struct rt_mailbox"] = build_mailbox_layout()
    if cfg.using_messagequeue:
        kl.structs["struct rt_messagequeue"] = build_messagequeue_layout()
    if cfg.using_memheap:
        kl.structs["struct rt_memheap"] = build_memheap_layout()
    if cfg.using_mempool:
        kl.structs["struct rt_mempool"] = build_mempool_layout()
    if cfg.using_memory_object:
        kl.structs["struct rt_memory"] = build_memory_layout()

    # List hooks
    kl.list_hooks = _build_list_hooks(cfg)

    # Object type registry
    kl.object_types = _build_object_types(cfg)

    return kl
