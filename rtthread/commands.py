"""RT-Thread aggregate GDB commands.

Only 5 commands, each handling what GDB expressions cannot easily do:
iterate collections and tabulate results.  Single-object field inspection
is delegated to convenience functions (``$gdr_thread``) + GDB expressions.

Commands
--------
- ``rtthread threads``        — list all threads
- ``rtthread semaphores``     — list semaphores
- ``rtthread timers``         — list timers
- ``rtthread objects [type]`` — list kernel objects, optionally filtered
- ``rtthread system``         — system summary (tick, scheduler, heap)
"""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from gdr.gdb_bridge import (
    eval_safe,
    gdb_command_guard,
    info,
    print_table,
    read_int,
    warn,
)
from gdr.layout import KernelLayout
from rtthread import adapter
from rtthread.layout import (
    RT_OBJECT_CLASS_DEVICE,
    RT_OBJECT_CLASS_EVENT,
    RT_OBJECT_CLASS_MAILBOX,
    RT_OBJECT_CLASS_MEMHEAP,
    RT_OBJECT_CLASS_MEMORY,
    RT_OBJECT_CLASS_MEMPOOL,
    RT_OBJECT_CLASS_MESSAGEQUEUE,
    RT_OBJECT_CLASS_MUTEX,
    RT_OBJECT_CLASS_SEMAPHORE,
    RT_OBJECT_CLASS_THREAD,
    RT_OBJECT_CLASS_TIMER,
    ThreadState,
)
from rtthread.navigation import (
    get_current_thread,
    get_tick,
    iter_objects,
    iter_threads,
    iter_timers,
)

# Module-level layout reference, set by register_commands()
_kl: KernelLayout | None = None
_registered = False

# Human-readable names for object type codes
_TYPE_NAMES: dict[int, str] = {
    RT_OBJECT_CLASS_THREAD: "thread",
    RT_OBJECT_CLASS_SEMAPHORE: "semaphore",
    RT_OBJECT_CLASS_MUTEX: "mutex",
    RT_OBJECT_CLASS_EVENT: "event",
    RT_OBJECT_CLASS_MAILBOX: "mailbox",
    RT_OBJECT_CLASS_MESSAGEQUEUE: "msgqueue",
    RT_OBJECT_CLASS_MEMHEAP: "memheap",
    RT_OBJECT_CLASS_MEMPOOL: "mempool",
    RT_OBJECT_CLASS_MEMORY: "memory",
    RT_OBJECT_CLASS_DEVICE: "device",
    RT_OBJECT_CLASS_TIMER: "timer",
}

_THREAD_STATE_NAMES: dict[int, str] = {
    int(ThreadState.INIT): "init",
    int(ThreadState.READY): "ready",
    int(ThreadState.SUSPEND): "suspend",
    int(ThreadState.RUNNING): "running",
    int(ThreadState.CLOSE): "close",
    int(ThreadState.UNKNOWN): "unknown",
}


def _state_name(state: int) -> str:
    """Map a ThreadState int to a human-readable name."""
    return _THREAD_STATE_NAMES.get(state, f"0x{state:x}")


def _addr_str(addr: int) -> str:
    """Format an address as hex string."""
    return hex(addr) if addr else "0x0"


@gdb_command_guard
def _cmd_threads() -> None:
    """List all threads in a table."""
    if _kl is None:
        warn("run `gdr init <rtos> <version>` to specify the RTOS and version first")
        return
    rows = []
    current = get_current_thread()
    current_addr = 0
    if current is not None and current.address:
        try:
            current_addr = int(current.address)
        except (TypeError, gdb.error):
            current_addr = 0

    for val in iter_threads(_kl):
        thr = adapter.value_to_thread(val, _kl)
        marker = " *" if thr.address == current_addr and current_addr else ""
        rows.append(
            [
                thr.name + marker,
                _state_name(thr.state),
                str(thr.current_priority),
                _addr_str(thr.sp),
                f"{thr.stack_size}" if thr.stack_size else "0",
                str(thr.stack_used) if thr.stack_used is not None else "N/A",
                str(thr.max_stack_used) if thr.max_stack_used is not None else "N/A",
                _addr_str(thr.entry),
            ]
        )
    print_table(
        rows,
        [
            "Name",
            "State",
            "Prio",
            "SP",
            "StkSize",
            "StkUsed",
            "MaxStkUsed",
            "Entry",
        ],
    )


@gdb_command_guard
def _cmd_semaphores() -> None:
    """List all semaphores in a table."""
    if _kl is None:
        warn("run `gdr init <rtos> <version>` to specify the RTOS and version first")
        return
    sl = _kl.structs.get("struct rt_semaphore")
    if sl is None:
        info("semaphore support not compiled in (RT_USING_SEMAPHORE)")
        return
    rows = []
    for val in iter_objects(RT_OBJECT_CLASS_SEMAPHORE, _kl):
        sem = adapter.value_to_semaphore(val, _kl)
        rows.append([sem.name, str(sem.value), _addr_str(sem.address)])
    print_table(rows, ["Name", "Value", "Addr"])


@gdb_command_guard
def _cmd_timers() -> None:
    """List all timers in a table."""
    if _kl is None:
        warn("run `gdr init <rtos> <version>` to specify the RTOS and version first")
        return

    tick = get_tick()
    info(f"Kernel tick: {tick if tick is not None else 'N/A'}")

    rows = []
    for val in iter_timers(_kl):
        timer = adapter.value_to_timer(val, _kl)
        active = "active" if timer.active else "inactive"
        mode = "periodic" if timer.periodic else "one-shot"
        kind = "soft" if timer.soft_timer else "hard"
        cb = _addr_str(timer.callback)
        rows.append(
            [
                timer.name,
                active,
                mode,
                kind,
                str(timer.init_tick),
                str(timer.timeout_tick),
                cb,
            ]
        )
    print_table(
        rows,
        ["Name", "State", "Mode", "Type", "InitTick", "TimeoutTick", "Callback"],
    )


@gdb_command_guard
def _cmd_objects(arg: str) -> None:
    """List kernel objects, optionally filtered by type name.

    Args:
        arg: Optional type name filter (e.g. ``"thread"``, ``"semaphore"``).
            If empty, lists counts of all enabled types.
    """
    if _kl is None:
        warn("run `gdr init <rtos> <version>` to specify the RTOS and version first")
        return

    if arg.strip():
        # Filter by type name
        type_code = _parse_type_name(arg.strip())
        if type_code is None:
            warn(f"unknown object type: {arg!r}")
            warn(f"valid types: {', '.join(sorted(_TYPE_NAMES.values()))}")
            return
        info_obj = _kl.object_types.get(type_code)
        if info_obj is None or not info_obj.enabled:
            warn(f"object type {arg!r} not enabled in this kernel config")
            return
        count = 0
        for _ in iter_objects(type_code, _kl):
            count += 1
        info(f"{_TYPE_NAMES[type_code]}: {count} object(s)")
    else:
        # Summary of all types
        rows = []
        for tc, name in sorted(_TYPE_NAMES.items()):
            ti = _kl.object_types.get(tc)
            if ti is None or not ti.enabled:
                continue
            count = sum(1 for _ in iter_objects(tc, _kl))
            rows.append([name, str(count)])
        print_table(rows, ["Type", "Count"])


def _parse_type_name(name: str) -> int | None:
    """Parse a human-readable type name to its type code."""
    name_lower = name.lower()
    for tc, tn in _TYPE_NAMES.items():
        if tn == name_lower:
            return tc
    return None


@gdb_command_guard
def _cmd_system() -> None:
    """Print system summary: tick, current thread, object counts, heap."""
    if _kl is None:
        warn("run `gdr init <rtos> <version>` to specify the RTOS and version first")
        return

    tick = get_tick()
    info(f"Kernel tick: {tick if tick is not None else 'N/A'}")

    current = get_current_thread()
    if current is not None:
        thr = adapter.value_to_thread(current, _kl)
        info(
            f"Current thread: {thr.name} (prio={thr.current_priority}, "
            f"state={_state_name(thr.state)})"
        )
    else:
        info("Current thread: N/A")

    # Object counts
    for tc, name in sorted(_TYPE_NAMES.items()):
        ti = _kl.object_types.get(tc)
        if ti is None or not ti.enabled:
            continue
        count = sum(1 for _ in iter_objects(tc, _kl))
        if count > 0:
            info(f"  {name}: {count}")

    # Heap info (best-effort)
    _print_heap_summary()


def _print_heap_summary() -> None:
    """Print heap usage summary if heap symbols are available."""
    # Reason: heap internals are allocator-specific (small_mem vs slab vs
    # memheap).  We probe the known public symbols rather than parsing
    # internal structs, keeping this robust across configurations.
    mem_total = eval_safe("(int)rt_memory_info(0)")
    if mem_total is not None:
        info(f"Heap: {read_int(mem_total)} bytes used")
        return
    info("Heap: details unavailable (no rt_memory_info symbol)")


# ---------------------------------------------------------------------------
# GDB command classes (guarded for import outside GDB)
# ---------------------------------------------------------------------------

if gdb is not None:

    class _RtThreadCmd(gdb.Command):
        """rtthread - RT-Thread debugging commands (GDR).

        Usage:
            rtthread threads          List all threads
            rtthread semaphores       List semaphores
            rtthread timers           List timers
            rtthread objects [type]   List kernel objects
            rtthread system           Show system summary
            rtthread help             Show this help
        """

        def __init__(self):
            super().__init__(
                "rtthread",
                gdb.COMMAND_USER,
                gdb.COMPLETE_COMMAND,
            )

        def invoke(self, arg_str: str, from_tty: bool) -> None:
            """Dispatch subcommand."""
            args = arg_str.split()
            if not args:
                self._print_help()
                return
            sub = args[0].lower()
            if sub == "help":
                self._print_help()
            elif sub == "threads":
                _cmd_threads()
            elif sub == "semaphores":
                _cmd_semaphores()
            elif sub == "timers":
                _cmd_timers()
            elif sub == "objects":
                _cmd_objects(" ".join(args[1:]))
            elif sub == "system":
                _cmd_system()
            else:
                warn(f"unknown subcommand: {sub!r}")
                self._print_help()

        def _print_help(self) -> None:
            """Print usage help."""
            print(self.__doc__)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_commands(kl: KernelLayout) -> None:
    """Register the ``rtthread`` GDB command and set the layout reference.

    Args:
        kl: Kernel layout built by ``rtthread.layout.build_layouts``.
    """
    global _kl
    _kl = kl

    register_command_shell()
    info("rtthread commands registered (alias: rtt)")


def register_command_shell() -> None:
    """Register the ``rtthread`` command shell before layouts are initialised."""
    global _registered

    if gdb is None:
        raise RuntimeError("not running inside GDB")

    if _registered:
        return

    _RtThreadCmd()
    gdb.execute("alias rtt = rtthread")
    _registered = True
