"""RT-Thread 3.1.x and 4.x kernel layout descriptions.

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
* **Minimal version profiles.** RT-Thread 3.1.3 inserted ``Null = 0`` into
  ``rt_object_class_type``. The profile retains this one semantic version
  boundary while the remaining layout choices stay configuration-driven.
* **Flat vs nested inheritance.**  ``rt_thread`` flattens ``rt_object`` fields
  directly (depth 0), while ``rt_timer`` embeds via ``parent`` (depth 1) and
  ``rt_semaphore`` embeds via ``parent.parent`` (depth 2, through
  ``rt_ipc_object``).  The ``_object_fields`` helper generates the right paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.layout import (
    KernelLayout,
    ListHook,
    ObjectTypeInfo,
    StructField,
    StructLayout,
)

# ---------------------------------------------------------------------------
# Constants — RT-Thread 4.x object type codes (rtdef.h enum
# rt_object_class_type). Runtime consumers must use ``KernelLayout.object_codes``
# because RT-Thread 3.1.0-3.1.2 used the preceding values.
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

# Event flag bits (rtdef.h)
RT_EVENT_FLAG_AND = 0x1
RT_EVENT_FLAG_OR = 0x2
RT_EVENT_FLAG_CLEAR = 0x4

# IPC scheduling policy bits (rtdef.h)
RT_IPC_FLAG_FIFO = 0x00
RT_IPC_FLAG_PRIO = 0x01

# Thread stat mask (rtdef.h)
RT_THREAD_STAT_MASK = 0x07

# Timer skip-list level (rtdef.h default is 1)
RT_TIMER_SKIP_LIST_LEVEL = 1
RT_THREAD_STACK_FILL = ord("#")


class ThreadState(IntEnum):
    """RT-Thread thread stat values (low bits of ``rt_thread.stat``)."""

    UNKNOWN = -1
    INIT = 0x00
    READY = 0x01
    SUSPEND = 0x02
    RUNNING = 0x03
    CLOSE = 0x04

    @classmethod
    def from_raw(cls, raw: int) -> ThreadState:
        """Map a raw stat byte to a known state, masking non-state bits."""
        try:
            return cls(raw & RT_THREAD_STAT_MASK)
        except ValueError:
            return cls.UNKNOWN


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

_OBJECT_TYPE_ORDER = (
    ("thread", "THREAD", "struct rt_thread", ("list",)),
    ("semaphore", "SEMAPHORE", "struct rt_semaphore", ("parent", "parent", "list")),
    ("mutex", "MUTEX", "struct rt_mutex", ("parent", "parent", "list")),
    ("event", "EVENT", "struct rt_event", ("parent", "parent", "list")),
    ("mailbox", "MAILBOX", "struct rt_mailbox", ("parent", "parent", "list")),
    (
        "msgqueue",
        "MSGQUEUE",
        "struct rt_messagequeue",
        ("parent", "parent", "list"),
    ),
    ("memheap", "MEMHEAP", "struct rt_memheap", ("parent", "list")),
    ("mempool", "MEMPOOL", "struct rt_mempool", ("parent", "list")),
    ("device", "DEVICE", "struct rt_device", ("parent", "list")),
    ("timer", "TIMER", "struct rt_timer", ("parent", "list")),
    # RT-Thread reserves OBJECT_UNKNOWN between TIMER and MEMORY.
    ("memory", "MEMORY", "struct rt_memory", ("parent", "list")),
)


def resolve_object_type_code(type_name: str, layout: KernelLayout) -> int | None:
    """Resolve a human type name to the active target's numeric object code.

    Accepts semantic names (``semaphore``) and display names (``SEMAPHORE``),
    case-insensitively, using the version-specific ``layout.object_codes``.
    """
    key = type_name.strip().lower()
    if not key:
        return None
    code = layout.object_codes.get(key)
    if code is not None:
        return code
    for type_code, info in layout.object_types.items():
        if info.name.lower() == key:
            return type_code
    return None


def _is_legacy_31(version: tuple[int, int, int]) -> bool:
    """Return whether a version predates the 3.1.3 NULL enum entry."""
    return (3, 1, 0) <= version <= (3, 1, 2)


def _probe_cpu_count(lookup_symbol) -> int | None:
    """Probe the SMP CPU count from target symbols.

    ``struct rt_cpu _cpus[RT_CPUS_NR]`` (4.x) or ``rt_cpus[]`` (3.1) is a
    global or file-static array whose length equals the configured CPU count.
    Falling back to ``RT_CPUS_NR`` only when the macro is visible; otherwise
    return ``None`` and callers treat unknown CPU values conservatively.
    """
    for symbol_name in ("_cpus", "_rt_cpus", "rt_cpus"):
        array = lookup_symbol(symbol_name)
        if array is None:
            continue
        try:
            array_type = array.type.strip_typedefs()
            if array_type.code == gdb.TYPE_CODE_ARRAY:
                low, high = array_type.range()
                return high - low + 1
        except (gdb.error, AttributeError, TypeError, ValueError):
            continue

    from gdr.gdb_bridge import read_macro_int

    count = read_macro_int("RT_CPUS_NR")
    if count is not None and count > 0:
        return count
    return None


def _messagequeue_has_sender_list(version: tuple[int, int, int]) -> bool:
    """Return whether ``rt_messagequeue`` has ``suspend_sender_thread``.

    The sender wait list was introduced in v3.1.4, dropped in v4.0.0-v4.0.1,
    and restored in v4.0.2.
    """
    if version[0] == 3:
        return (3, 1, 4) <= version <= (3, 1, 5)
    return version >= (4, 0, 2) and version[0] == 4


def _object_codes(version: tuple[int, int, int]) -> dict[str, int]:
    """Build semantic object type codes for an RT-Thread version profile."""
    offset = 0 if _is_legacy_31(version) else 1
    codes = {
        name: index + offset for index, (name, *_rest) in enumerate(_OBJECT_TYPE_ORDER)
    }
    # OBJECT_UNKNOWN occupies the code immediately before MEMORY.
    codes["memory"] += 1
    return codes


def _object_type_names(codes: dict[str, int]) -> dict[int, str]:
    """Build numeric enum display names for the active object profile."""
    return {
        codes[name]: enum_name
        for name, enum_name, _struct_name, _list_path in _OBJECT_TYPE_ORDER
    }


# Thread stat → display name (low 3 bits of rt_thread.stat).
THREAD_STAT_NAMES: dict[int, str] = {
    int(ThreadState.INIT): "INIT",
    int(ThreadState.READY): "READY",
    int(ThreadState.SUSPEND): "SUSPEND",
    int(ThreadState.RUNNING): "RUNNING",
    int(ThreadState.CLOSE): "CLOSE",
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
    using_soft_timer: bool = False
    using_memory_object: bool = False
    thread_has_init_priority: bool = True
    thread_has_pthread_data: bool = False
    heap_type: str = "none"  # "small_mem", "slab", "memheap", "none"
    using_memtrace: bool = False
    stack_grows_up: bool | None = None
    cpu_count: int | None = None


def _gdb_type_labels(typ) -> list[str]:
    """Return GDB ``Type.name`` / ``Type.tag`` strings that identify *typ*.

    Typedef names live on the ``TYPE_CODE_TYPEDEF`` object. After
    ``strip_typedefs()`` a pointer type often has an empty name, with the
    struct identity on the pointer target (and sometimes only on ``tag``).
    """
    labels: list[str] = []
    for attr in ("name", "tag"):
        value = getattr(typ, attr, None)
        if value:
            labels.append(value)
    return labels


def _system_heap_allocator(system_heap, symbol_exists) -> str | None:
    """Classify the system-heap algorithm from a 4.1 ``system_heap`` handle.

    The handle is either a plain ``struct rt_memheap`` (memheap as heap) or a
    pointer typedef ``rt_smem_t`` / ``rt_slab_t`` that aliases
    ``struct rt_memory *``. GDB exposes those aliases as ``TYPE_CODE_TYPEDEF``
    (the typedef name) wrapping a nameless pointer, so the declared type is
    recorded before ``strip_typedefs()``. Only a concrete ``rt_slab_t`` /
    ``struct rt_slab`` handle wins over the small_mem default because small_mem
    and slab share the ``struct rt_memory *`` handle shape.
    """
    if system_heap is None:
        return None
    try:
        heap_type = system_heap.type
        labels = _gdb_type_labels(heap_type)
        stripped = heap_type.strip_typedefs()
        labels.extend(_gdb_type_labels(stripped))
        if stripped.code == gdb.TYPE_CODE_PTR:
            labels.extend(_gdb_type_labels(stripped.target().strip_typedefs()))
    except (gdb.error, AttributeError, TypeError, ValueError):
        return None
    joined = "\n".join(labels)
    if "rt_memheap" in joined:
        return "memheap"
    if "rt_slab" in joined:
        return "slab"
    if "rt_smem" in joined or "rt_memory" in joined or "rt_small_mem" in joined:
        return "small_mem"
    if symbol_exists("rt_smem_init"):
        return "small_mem"
    return None


def _probe_using_memtrace(lookup_type, symbol_exists) -> bool:
    """Probe RT_USING_MEMTRACE from block-header DWARF, then fallback symbols.

    The authoritative check is a ``thread`` / ``owner_thread_name`` member on a
    heap block header; the exported ``memtrace`` / ``memcheck`` functions only
    exist when FINSH is compiled, so symbol presence alone is insufficient.
    """
    for type_name in (
        "struct heap_mem",
        "struct rt_small_mem_item",
        "struct rt_memheap_item",
    ):
        type_info = lookup_type(type_name)
        if type_info is None:
            continue
        try:
            field_names = {f.name for f in type_info.fields()}
        except (gdb.error, AttributeError, TypeError):
            field_names = set()
        if "thread" in field_names or "owner_thread_name" in field_names:
            return True
    return symbol_exists("memtrace") or symbol_exists("memcheck")


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
    from gdr.gdb_bridge import lookup_symbol, lookup_type, macro_defined, symbol_exists

    cfg = RtConfig()

    cfg.smp = symbol_exists("rt_cpu_index")
    if cfg.smp:
        cfg.cpu_count = _probe_cpu_count(lookup_symbol)
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
    # Reason: soft timers are optional and are unrelated to device support.
    # Their exported/list symbols are stable across the supported 3.1 and 4.x
    # branches, while the list name itself changed between those series.
    cfg.using_soft_timer = symbol_exists("rt_soft_timer_list") or symbol_exists(
        "_soft_timer_list"
    )
    # Reason: absent macro debug information cannot prove a target stack grows
    # downward. The adapter infers it from RT-Thread's stack-fill sentinels.
    cfg.stack_grows_up = True if macro_defined("ARCH_CPU_STACK_GROWS_UPWARD") else None

    cfg.using_memory_object = lookup_type("struct rt_memory") is not None

    # Reason: heap_type prefers the 4.1 ``system_heap`` handle (a typed object
    # or pointer typedef) so the active ``*_AS_HEAP`` allocator wins even when
    # several heap algorithms are compiled into the kernel. 4.0 kernels expose
    # the system heap as static globals instead, so they fall back to
    # ``memusage`` / ``heap_end`` / ``_heap`` symbol presence.
    heap_type = _system_heap_allocator(lookup_symbol("system_heap"), symbol_exists)
    if heap_type is None:
        if symbol_exists("memusage"):
            heap_type = "slab"
        elif symbol_exists("heap_end"):
            heap_type = "small_mem"
        elif symbol_exists("_heap"):
            heap_type = "memheap"
        else:
            heap_type = "none"
    cfg.heap_type = heap_type

    cfg.using_memtrace = _probe_using_memtrace(lookup_type, symbol_exists)

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


def _object_fields(
    depth: int, object_type_names: dict[int, str], flag_field: str = "flag"
) -> dict[str, StructField]:
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
            "type", (*p, "type"), kind="enum", enum_map=object_type_names
        ),
        "flag": StructField("flag", (*p, flag_field), kind="flags"),
        "list": StructField("list", (*p, "list"), kind="list"),
    }


def _ipc_fields(object_type_names: dict[int, str]) -> dict[str, StructField]:
    """Fields for structs inheriting from ``rt_ipc_object`` (depth 2)."""
    fields = _object_fields(2, object_type_names)
    fields["suspend_thread"] = StructField(
        "suspend_thread", ("parent", "suspend_thread"), kind="list"
    )
    # Reason: IPC scheduling policy lives in ``rt_object.flag`` (0=FIFO, 1=PRIO).
    # Decoding stays in the adapter's ``_ipc_policy``; the pretty-printer calls
    # it through ``StructField.formatter`` so the RTOS-neutral core reuses the
    # same decoder instead of duplicating the mapping as an enum_map.
    fields["policy"] = StructField(
        "policy",
        ("parent", "parent", "flag"),
        summary=True,
    )
    return fields


# ---------------------------------------------------------------------------
# Struct layout builders
# ---------------------------------------------------------------------------


def build_thread_layout(
    cfg: RtConfig,
    version: tuple[int, int, int],
    object_type_names: dict[int, str],
) -> StructLayout:
    """Build ``rt_thread`` layout (COUPLED: rtdef.h struct rt_thread).

    ``rt_thread`` flattens ``rt_object`` fields directly — no ``parent``
    embedding.  SMP adds ``bind_cpu`` / ``oncpu`` / lock-nest counters.
    """
    sl = StructLayout("struct rt_thread", display_name="Thread")
    f = sl.fields

    # Flat rt_object fields (depth 0)
    thread_flag_field = "flags" if version[0] == 3 else "flag"
    f.update(_object_fields(0, object_type_names, thread_flag_field))

    # Thread scheduling list (separate from object list)
    f["tlist"] = StructField("tlist", ("tlist",), kind="list")

    # Stack and entry
    f["sp"] = StructField("sp", ("sp",), kind="ptr", summary=True)
    f["entry"] = StructField("entry", ("entry",), kind="ptr", summary=True)
    f["parameter"] = StructField("parameter", ("parameter",), kind="ptr")
    f["stack_addr"] = StructField("stack_addr", ("stack_addr",), kind="ptr")
    f["stack_size"] = StructField("stack_size", ("stack_size",), summary=True)

    # Error and state
    f["error"] = StructField("error", ("error",))
    f["stat"] = StructField(
        "stat", ("stat",), kind="enum", summary=True, enum_map=THREAD_STAT_NAMES
    )

    # SMP-conditional fields
    if cfg.smp:
        f["bind_cpu"] = StructField("bind_cpu", ("bind_cpu",), summary=True)
        f["oncpu"] = StructField("oncpu", ("oncpu",), summary=True)
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
        f["init_priority"] = StructField(
            "init_priority", ("init_priority",), summary=True
        )
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
        f["event_set"] = StructField("event_set", ("event_set",))
        f["event_info"] = StructField("event_info", ("event_info",))
    if cfg.using_cpu_usage:
        f["duration_tick"] = StructField("duration_tick", ("duration_tick",))
    if cfg.thread_has_pthread_data:
        f["pthread_data"] = StructField("pthread_data", ("pthread_data",), kind="ptr")

    return sl


def build_timer_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_timer`` layout (COUPLED: rtdef.h struct rt_timer)."""
    sl = StructLayout("struct rt_timer", display_name="Timer")
    sl.fields.update(_object_fields(1, object_type_names))  # parent = rt_object
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
        "timeout_func", ("timeout_func",), kind="ptr", summary=True
    )
    sl.fields["parameter"] = StructField("parameter", ("parameter",), kind="ptr")
    sl.fields["init_tick"] = StructField("init_tick", ("init_tick",), summary=True)
    sl.fields["timeout_tick"] = StructField(
        "timeout_tick", ("timeout_tick",), summary=True
    )
    return sl


def build_semaphore_layout(
    version: tuple[int, int, int], object_type_names: dict[int, str]
) -> StructLayout:
    """Build ``rt_semaphore`` layout (COUPLED: rtdef.h struct rt_semaphore)."""
    sl = StructLayout("struct rt_semaphore", display_name="Semaphore")
    sl.fields.update(_ipc_fields(object_type_names))  # parent.parent = rt_object
    sl.fields["value"] = StructField("value", ("value",), summary=True)
    if version >= (3, 1, 3):
        sl.fields["reserved"] = StructField("reserved", ("reserved",))
    return sl


def build_mutex_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_mutex`` layout (COUPLED: rtdef.h struct rt_mutex)."""
    sl = StructLayout("struct rt_mutex", display_name="Mutex")
    sl.fields.update(_ipc_fields(object_type_names))
    sl.fields["value"] = StructField("value", ("value",), summary=True)
    sl.fields["original_priority"] = StructField(
        "original_priority", ("original_priority",), summary=True
    )
    sl.fields["hold"] = StructField("hold", ("hold",), summary=True)
    sl.fields["owner"] = StructField(
        "owner",
        ("owner",),
        kind="ptr",
        summary=True,
        pointee_string_path=("name",),
    )
    return sl


def build_event_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_event`` layout (COUPLED: rtdef.h struct rt_event)."""
    sl = StructLayout("struct rt_event", display_name="Event")
    sl.fields.update(_ipc_fields(object_type_names))
    # Reason: the event bit-set is a mask, so hex keeps the individual bits
    # readable and matches the ``rtt events`` table's ``Set`` column.
    sl.fields["set"] = StructField("set", ("set",), kind="hex", summary=True)
    return sl


def build_mailbox_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_mailbox`` layout (COUPLED: rtdef.h struct rt_mailbox)."""
    sl = StructLayout("struct rt_mailbox", display_name="Mailbox")
    sl.fields.update(_ipc_fields(object_type_names))
    sl.fields["msg_pool"] = StructField("msg_pool", ("msg_pool",), kind="ptr")
    sl.fields["size"] = StructField("size", ("size",), summary=True)
    sl.fields["entry"] = StructField("entry", ("entry",), summary=True)
    sl.fields["in_offset"] = StructField("in_offset", ("in_offset",), summary=True)
    sl.fields["out_offset"] = StructField("out_offset", ("out_offset",), summary=True)
    sl.fields["suspend_sender_thread"] = StructField(
        "suspend_sender_thread", ("suspend_sender_thread",), kind="list"
    )
    return sl


def build_messagequeue_layout(
    object_type_names: dict[int, str], version: tuple[int, int, int] = (4, 1, 1)
) -> StructLayout:
    """Build ``rt_messagequeue`` layout (COUPLED: rtdef.h struct rt_messagequeue).

    ``suspend_sender_thread`` is version-conditional: it is absent before
    v3.1.4 and in v4.0.0-v4.0.1, and present elsewhere.
    """
    sl = StructLayout("struct rt_messagequeue", display_name="MsgQueue")
    sl.fields.update(_ipc_fields(object_type_names))
    sl.fields["msg_pool"] = StructField("msg_pool", ("msg_pool",), kind="ptr")
    sl.fields["msg_size"] = StructField("msg_size", ("msg_size",), summary=True)
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
    if _messagequeue_has_sender_list(version):
        sl.fields["suspend_sender_thread"] = StructField(
            "suspend_sender_thread", ("suspend_sender_thread",), kind="list"
        )
    return sl


def build_memheap_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_memheap`` layout (COUPLED: rtdef.h struct rt_memheap)."""
    sl = StructLayout("struct rt_memheap", display_name="MemHeap")
    sl.fields.update(_object_fields(1, object_type_names))  # parent = rt_object
    sl.fields["start_addr"] = StructField("start_addr", ("start_addr",), kind="ptr")
    sl.fields["pool_size"] = StructField("pool_size", ("pool_size",), summary=True)
    sl.fields["available_size"] = StructField(
        "available_size", ("available_size",), summary=True
    )
    sl.fields["max_used_size"] = StructField("max_used_size", ("max_used_size",))
    return sl


def build_mempool_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_mempool`` layout (COUPLED: rtdef.h struct rt_mempool)."""
    sl = StructLayout("struct rt_mempool", display_name="MemPool")
    sl.fields.update(_object_fields(1, object_type_names))  # parent = rt_object
    sl.fields["start_address"] = StructField(
        "start_address", ("start_address",), kind="ptr"
    )
    sl.fields["size"] = StructField("size", ("size",), summary=True)
    sl.fields["block_size"] = StructField("block_size", ("block_size",), summary=True)
    sl.fields["block_total_count"] = StructField(
        "block_total_count", ("block_total_count",), summary=True
    )
    sl.fields["block_free_count"] = StructField(
        "block_free_count", ("block_free_count",), summary=True
    )
    # Reason: block_list is the free-block singly-linked list head; it is only
    # consumed by detail diagnostics, never by the default table.
    sl.fields["block_list"] = StructField("block_list", ("block_list",), kind="ptr")
    # Reason: waiters are counted by traversing the suspend list on every
    # version; the cached suspend_thread_count was removed in later releases
    # so the layout must describe the list itself.
    sl.fields["suspend_thread"] = StructField(
        "suspend_thread", ("suspend_thread",), kind="list"
    )
    return sl


def build_memory_layout(object_type_names: dict[int, str]) -> StructLayout:
    """Build ``rt_memory`` layout (RT-Thread 4.1.x heap object)."""
    sl = StructLayout("struct rt_memory")
    sl.fields.update(_object_fields(1, object_type_names))  # parent = rt_object
    sl.fields["algorithm"] = StructField("algorithm", ("algorithm",), kind="string")
    sl.fields["address"] = StructField("address", ("address",), kind="ptr")
    sl.fields["total"] = StructField("total", ("total",), summary=True)
    sl.fields["used"] = StructField("used", ("used",), summary=True)
    sl.fields["max"] = StructField("max", ("max",), summary=True)
    return sl


def build_heap_mem_layout(cfg: RtConfig) -> StructLayout:
    """Build the 4.0 small_mem block header ``struct heap_mem``.

    ``thread`` is config-conditional: RT_USING_MEMTRACE adds the owner-name
    array between the prev/next links and the data payload.
    """
    sl = StructLayout("struct heap_mem")
    sl.fields["magic"] = StructField("magic", ("magic",))
    sl.fields["used"] = StructField("used", ("used",))
    sl.fields["next"] = StructField("next", ("next",))
    sl.fields["prev"] = StructField("prev", ("prev",))
    if cfg.using_memtrace:
        sl.fields["thread"] = StructField("thread", ("thread",), kind="string")
    return sl


def build_small_mem_layout() -> StructLayout:
    """Build the 4.1 small_mem object ``struct rt_small_mem`` (COUPLED)."""
    sl = StructLayout("struct rt_small_mem")
    sl.fields["heap_ptr"] = StructField("heap_ptr", ("heap_ptr",), kind="ptr")
    sl.fields["heap_end"] = StructField("heap_end", ("heap_end",), kind="ptr")
    sl.fields["lfree"] = StructField("lfree", ("lfree",), kind="ptr")
    sl.fields["mem_size_aligned"] = StructField(
        "mem_size_aligned", ("mem_size_aligned",)
    )
    return sl


def build_small_mem_item_layout(cfg: RtConfig) -> StructLayout:
    """Build the 4.1 small_mem block header ``struct rt_small_mem_item``.

    The used flag is the LSB of ``pool_ptr``; ``thread`` is MEMTRACE-conditional
    exactly like ``struct heap_mem``.
    """
    sl = StructLayout("struct rt_small_mem_item")
    sl.fields["pool_ptr"] = StructField("pool_ptr", ("pool_ptr",))
    sl.fields["next"] = StructField("next", ("next",))
    sl.fields["prev"] = StructField("prev", ("prev",))
    if cfg.using_memtrace:
        sl.fields["thread"] = StructField("thread", ("thread",), kind="string")
    return sl


def build_memheap_item_layout(cfg: RtConfig) -> StructLayout:
    """Build the memheap block header ``struct rt_memheap_item`` (COUPLED).

    ``owner_thread_name`` is MEMTRACE-conditional and carries the allocating
    thread's name for per-thread occupancy diagnostics.
    """
    sl = StructLayout("struct rt_memheap_item")
    sl.fields["magic"] = StructField("magic", ("magic",))
    sl.fields["pool_ptr"] = StructField("pool_ptr", ("pool_ptr",), kind="ptr")
    sl.fields["next"] = StructField("next", ("next",), kind="ptr")
    sl.fields["prev"] = StructField("prev", ("prev",), kind="ptr")
    if cfg.using_memtrace:
        sl.fields["owner_thread_name"] = StructField(
            "owner_thread_name", ("owner_thread_name",), kind="string"
        )
    return sl


def build_slab_layout() -> StructLayout:
    """Build the 4.1 slab object ``struct rt_slab`` fields used by page walks."""
    sl = StructLayout("struct rt_slab")
    sl.fields["heap_start"] = StructField("heap_start", ("heap_start",))
    sl.fields["heap_end"] = StructField("heap_end", ("heap_end",))
    sl.fields["memusage"] = StructField("memusage", ("memusage",), kind="ptr")
    return sl


def build_object_information_layout() -> StructLayout:
    """Build ``rt_object_information`` layout (COUPLED: rtdef.h)."""
    sl = StructLayout("struct rt_object_information")
    sl.fields["type"] = StructField("type", ("type",), kind="enum")
    sl.fields["object_list"] = StructField("object_list", ("object_list",), kind="list")
    sl.fields["object_size"] = StructField("object_size", ("object_size",))
    return sl


def object_information_layout(kl: KernelLayout) -> StructLayout | None:
    """Return the RT-Thread object-registry layout from a kernel layout."""
    return kl.structs.get("struct rt_object_information")


# ---------------------------------------------------------------------------
# Object type registry and list hooks
# ---------------------------------------------------------------------------

# Type code -> (struct_name, list paths) for container_of when iterating the
# object registry. The paths describe the embedded list node and its next link.
_OBJECT_LIST_NEXT_PATH = ("next",)


def _object_type(
    type_code: int,
    name: str,
    struct_name: str,
    list_path: tuple[str | int, ...],
) -> ObjectTypeInfo:
    """Build object registry metadata for RT-Thread's intrusive lists."""
    return ObjectTypeInfo(
        type_code,
        struct_name,
        list_path,
        next_path=_OBJECT_LIST_NEXT_PATH,
        name=name,
    )


def _build_object_types(
    cfg: RtConfig, codes: dict[str, int]
) -> dict[int, ObjectTypeInfo]:
    """Build the object registry for the active version and configuration."""
    enabled_map = {
        "semaphore": cfg.using_semaphore,
        "mutex": cfg.using_mutex,
        "event": cfg.using_event,
        "mailbox": cfg.using_mailbox,
        "msgqueue": cfg.using_messagequeue,
        "memheap": cfg.using_memheap,
        "mempool": cfg.using_mempool,
        "device": cfg.using_device,
        "memory": cfg.using_memory_object,
    }
    result: dict[int, ObjectTypeInfo] = {}
    for name, _enum_name, struct_name, list_path in _OBJECT_TYPE_ORDER:
        type_code = codes[name]
        result[type_code] = _object_type(type_code, name, struct_name, list_path)
        result[type_code].enabled = enabled_map.get(name, True)
    return result


def _build_list_hooks(
    cfg: RtConfig, version: tuple[int, int, int]
) -> dict[str, ListHook]:
    """Build list hooks for timer lists and priority table."""
    hooks: dict[str, ListHook] = {}

    # Hard timer list (timer.c: static rt_list_t _timer_list[1])
    timer_list_name = "rt_timer_list" if version[0] == 3 else "_timer_list"
    hooks["timer_list"] = ListHook(
        head_symbol=timer_list_name,
        node_path=("row", RT_TIMER_SKIP_LIST_LEVEL - 1),
        container_type="struct rt_timer",
        next_path=_OBJECT_LIST_NEXT_PATH,
        head_index=RT_TIMER_SKIP_LIST_LEVEL - 1,
    )

    # Soft timer list (only if soft timer is compiled in)
    if cfg.using_soft_timer:
        soft_timer_list_name = (
            "rt_soft_timer_list" if version[0] == 3 else "_soft_timer_list"
        )
        hooks["soft_timer_list"] = ListHook(
            head_symbol=soft_timer_list_name,
            node_path=("row", RT_TIMER_SKIP_LIST_LEVEL - 1),
            container_type="struct rt_timer",
            next_path=_OBJECT_LIST_NEXT_PATH,
            head_index=RT_TIMER_SKIP_LIST_LEVEL - 1,
        )

    return hooks


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_layouts(
    cfg: RtConfig, version: tuple[int, int, int] = (4, 1, 1)
) -> KernelLayout:
    """Assemble a complete ``KernelLayout`` from the probed configuration.

    This is the main entry point for the RT-Thread adapter. The resulting
    ``KernelLayout`` is passed to ``rtthread.navigation`` and
    ``gdr.printers`` for traversal and pretty-printer registration.

    Args:
        cfg: Probed ``RtConfig`` from ``detect_config``.
        version: Parsed RT-Thread version selected by the user. The default
            preserves the historical 4.x behavior for direct API callers.

    Returns:
        ``KernelLayout`` with struct layouts, list hooks, and object types.
    """
    object_codes = _object_codes(version)
    object_type_names = _object_type_names(object_codes)
    kl = KernelLayout(
        stack_grows_up=cfg.stack_grows_up,
        cpu_count=cfg.cpu_count,
        object_codes=object_codes,
    )

    # Struct layouts
    kl.structs["struct rt_thread"] = build_thread_layout(
        cfg, version, object_type_names
    )
    kl.structs["struct rt_timer"] = build_timer_layout(object_type_names)
    kl.structs["struct rt_object_information"] = build_object_information_layout()

    if cfg.using_semaphore:
        kl.structs["struct rt_semaphore"] = build_semaphore_layout(
            version, object_type_names
        )
    if cfg.using_mutex:
        kl.structs["struct rt_mutex"] = build_mutex_layout(object_type_names)
    if cfg.using_event:
        kl.structs["struct rt_event"] = build_event_layout(object_type_names)
    if cfg.using_mailbox:
        kl.structs["struct rt_mailbox"] = build_mailbox_layout(object_type_names)
    if cfg.using_messagequeue:
        kl.structs["struct rt_messagequeue"] = build_messagequeue_layout(
            object_type_names, version
        )
    if cfg.using_memheap:
        kl.structs["struct rt_memheap"] = build_memheap_layout(object_type_names)
    if cfg.using_mempool:
        kl.structs["struct rt_mempool"] = build_mempool_layout(object_type_names)
    if cfg.using_memory_object:
        kl.structs["struct rt_memory"] = build_memory_layout(object_type_names)

    # Heap block-header layouts follow the probed system allocator. Walks
    # already degrade via ``structs.get``; omitting unused types keeps
    # pretty-printers from claiming structs the target never compiled.
    if cfg.heap_type == "small_mem":
        if cfg.using_memory_object:
            kl.structs["struct rt_small_mem"] = build_small_mem_layout()
            kl.structs["struct rt_small_mem_item"] = build_small_mem_item_layout(cfg)
        else:
            kl.structs["struct heap_mem"] = build_heap_mem_layout(cfg)
    elif cfg.heap_type == "memheap":
        kl.structs["struct rt_memheap_item"] = build_memheap_item_layout(cfg)
    elif cfg.heap_type == "slab" and cfg.using_memory_object:
        kl.structs["struct rt_slab"] = build_slab_layout()

    # List hooks
    kl.list_hooks = _build_list_hooks(cfg, version)

    # Object type registry
    kl.object_types = _build_object_types(cfg, object_codes)

    return kl
