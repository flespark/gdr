"""FreeRTOS kernel-object decoders: software timers, event groups, stream buffers.

Per-object-kind decode shared by the adapter, detail builders and diagnostics:
the timer daemon queue and wrap-safe expiry, event-group wait conditions, and
stream/message/batching ring geometry; column contracts live in
:mod:`freertos.views`.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from core.adapter import (
    ObjectTable,
    address_cell,
    number_cell,
    raw_cell,
    text_cell,
)
from core.constants import GDR_MAX_TRAVERSAL_COUNT
from core.formatting import (
    format_address,
    format_optional_int,
    format_symbol_or_address,
    format_table,
)
from core.formatting import timer_expires_in as derive_timer_expires_in
from core.gdb_bridge import (
    TARGET_ACCESS_ERRORS,
    get_arch_info,
    lookup_symbol,
    lookup_symbol_at,
    lookup_type,
    read_bytes,
    read_cstring,
    read_int,
    safe_dereference,
    safe_int,
    type_field_names,
    type_size,
    value_address,
    warn,
)
from core.layout import read_field, value_at
from freertos.layout import FreeRtosLayout
from freertos.navigation import (
    current_tasks,
    iter_queue_items,
    mapped_ranges,
    source_label,
    task_name_at,
)
from freertos.views import EVENT_GROUP_SPECS, STREAM_BUFFER_SPECS

# ---------------------------------------------------------------------------
# Software-timer decode.
# ---------------------------------------------------------------------------
# timers.c tmrSTATUS_* bit definitions (ucStatus).
_TMR_STATUS_ACTIVE = 0x01
_TMR_STATUS_AUTORELOAD = 0x04

# Command ids for the daemon queue (include/timers.h, tmrCOMMAND_*).  The
# daemon switches on these in prvProcessReceivedCommands; ids outside this
# map render as ``unknown(<n>)``.
_TIMER_COMMAND_NAMES: dict[int, str] = {
    -2: "execute-callback-from-isr",
    -1: "execute-callback",
    0: "start-dont-trace",
    1: "start",
    2: "reset",
    3: "stop",
    4: "change-period",
    5: "delete",
    6: "start-from-isr",
    7: "reset-from-isr",
    8: "stop-from-isr",
    9: "change-period-from-isr",
}


@dataclass
class FreeRtosTimerObject:
    """Display model for one software timer (struct tmrTimerControl).

    ``expiry`` is the list item's xItemValue (the absolute expiry tick); it
    is only meaningful while the timer is linked into an active list -- a
    dormant timer keeps a stale value, so the renderer shows ``N/A``.
    ``container``/``owner`` are the list item's membership fields; the owner
    is heap garbage on a never-linked timer, so the OwnerCheck is decided by
    the container (freertos.timers.owner_check) and never by the owner value
    itself.
    """

    name: str = "-"
    address: int = 0
    source: str = ""
    extra_sources: tuple[str, ...] = ()
    period: int | None = None
    status: int | None = None
    expiry: int | None = None
    callback: int | None = None
    id: int | None = None
    timer_number: int | None = None
    owner: int | None = None
    container: int | None = None


@dataclass
class FreeRtosQueueObject:
    """Display model for one queue-family object (queue/semaphore/mutex).

    Scalar fields that are unreadable stay ``None`` and render as ``N/A``;
    ``free`` is ``max(length - count, 0)`` and only set when both are known.
    Mutex-only fields (``holder``/``recursive_count``) are decoded strictly
    for ``kind == "mutex"`` -- a semaphore's ``u`` union member holds
    QueuePointers_t and must never be read as SemaphoreData_t.
    """

    name: str = "-"
    address: int = 0
    kind: str = "queue"
    inferred_kind: bool = False
    type_code: int | None = None
    source: str = ""
    extra_sources: tuple[str, ...] = ()
    length: int | None = None
    item_size: int | None = None
    count: int | None = None
    free: int | None = None
    send_waiters: list[str] | None = None
    recv_waiters: list[str] | None = None
    rx_lock: int | None = None
    tx_lock: int | None = None
    set_container: int | None = None
    holder: str | None = None
    holder_address: int | None = None
    recursive_count: int | None = None


@dataclass(frozen=True)
class TimerCommand:
    """One decoded entry from the daemon command queue.

    ``seq`` is the FIFO ring index (0 = oldest pending); ``message_id`` is
    ``xMessageID`` (``None`` when the slot could not be read).  Timer-arm
    commands carry ``timer_address``/``message_value``; a pended callback
    (``message_id < 0``) leaves them ``None`` and carries the callback
    ``xCallbackParameters`` instead (``callback_function``,
    ``callback_parameter1``, ``callback_parameter2``) plus the
    ``callback_arm`` flag (whether the union arm exists in DWARF at all --
    when it does not, the kernel cannot have enqueued a pended callback).
    """

    seq: int
    message_id: int | None
    timer_address: int | None = None
    message_value: int | None = None
    callback_arm: bool = False
    callback_function: int | None = None
    callback_parameter1: int | None = None
    callback_parameter2: int | None = None


def command_name(message_id: int) -> str:
    """Render one daemon command id, ``unknown(<n>)`` outside the known map."""
    return _TIMER_COMMAND_NAMES.get(message_id, f"unknown({message_id})")


def timer_subsystem_ready() -> bool:
    """Whether the timer subsystem has been initialised.

    ``pxCurrentTimerList`` is a file-static zero-initialised pointer that
    only ``prvCheckForValidListAndQueue`` assigns (timers.c); ``NULL``
    means the daemon has not run yet, so there is no list to walk and the
    command queue may not exist either.
    """
    return safe_dereference(lookup_symbol("pxCurrentTimerList")) is not None


def _timer_list_value(name: str):
    """Dereference a ``static List_t *`` timer-list pointer, or ``None``."""
    return safe_dereference(lookup_symbol(name))


def timer_epoch(container: int | None) -> str | None:
    """Return ``"current"``/``"overflow"`` for a linked item, else ``None``.

    The container member of a linked ``xTimerListItem`` points at the
    active ``List_t``; the two list *pointers* swap when the tick counter
    overflows (timers.c ``prvSwitchTimerLists``), so the epoch is always
    resolved against the current pointer values.
    """
    if not container:
        return None
    current = _timer_list_value("pxCurrentTimerList")
    overflow = _timer_list_value("pxOverflowTimerList")
    if current is not None and container == value_address(current):
        return "current"
    if overflow is not None and container == value_address(overflow):
        return "overflow"
    return None


def timer_list_label(container: int | None) -> str:
    """Render a linked timer's list as ``current(xActiveTimerList1)`` etc.

    list1/list2 swap roles when the tick counter overflows, so the label
    resolves which *symbol* the current/overflow pointers actually point
    at -- a bare ``list1`` would silently flip meaning between sessions.
    """
    epoch = timer_epoch(container)
    if epoch is None:
        return "none" if not container else "other"
    resolved = _timer_list_value(f"px{epoch.capitalize()}TimerList")
    name = None
    for symbol, label in (
        (lookup_symbol("xActiveTimerList1"), "xActiveTimerList1"),
        (lookup_symbol("xActiveTimerList2"), "xActiveTimerList2"),
    ):
        if (
            symbol is not None
            and resolved is not None
            and value_address(symbol) == value_address(resolved)
        ):
            name = label
            break
    return f"{epoch}({name})" if name else epoch


def owner_check(container: int | None, owner: int | None, obj_address: int) -> str:
    """Verify the list item's ``pvOwner`` against the timer's own address.

    ``vListInitialiseItem`` only writes ``pxContainer = NULL`` (list.c) and
    leaves ``pvOwner`` untouched, so a never-linked timer's owner is heap
    garbage -- the container is the only trustworthy membership signal.
    That is why ``uninitialised`` is decided by the container and the owner
    value is never rendered for it.
    """
    if not container:
        return "uninitialised"
    if owner is None:
        return "unreadable"
    if owner == obj_address:
        return "ok"
    return f"mismatch (pvOwner={hex(owner)})"


def timer_is_on_lists(source: str, extra_sources: tuple[str, ...] = ()) -> bool:
    """Whether a timer was found by the active-list channel.

    The active channel (``pxCurrentTimerList`` / ``pxOverflowTimerList``) is
    the only channel that proves list membership.  It is usually the channel
    of record; on a build with the MPU wrappers pool the pool claims every
    object first and the active channel only corroborates (``extra_sources``
    carries ``"active"``), so membership must be checked across both.
    """
    return source == "active" or "active" in extra_sources


def state_cell(
    source: str, status: int | None, extra_sources: tuple[str, ...] = ()
) -> str:
    """Render the State cell: ``active``/``dormant``/``unknown``.

    The daemon updates ``ucStatus`` and the lists together while processing
    commands (timers.c prvProcessReceivedCommands); a queued START or STOP
    leaves them disagreeing.  A ``?`` suffix marks that in-flight
    transition instead of guessing which side wins.
    """
    if status is None:
        return "unknown"
    active = bool(status & _TMR_STATUS_ACTIVE)
    linked = timer_is_on_lists(source, extra_sources)
    if linked and active:
        return "active"
    if not linked and not active:
        return "dormant"
    return "active?" if linked else "dormant?"


def mode_cell(status: int | None) -> str:
    """Render the Mode cell: ``auto`` (periodic) vs ``one-shot``."""
    if status is None:
        return "N/A"
    return "auto" if status & _TMR_STATUS_AUTORELOAD else "one-shot"


def callback_cell(callback: int | None) -> str:
    """Render the Callback cell: ``<symbol>`` when resolvable, else hex."""
    if not callback:
        return "-"
    return format_symbol_or_address(callback, lookup_symbol_at(callback))


def id_cell(value: int | None) -> str:
    """Render the pvTimerID cell; the fixtures pass ``NULL``, so 0 is a dash."""
    if not value:
        return "-"
    return format_address(value)


def timer_expires_in(
    expiry: int | None,
    tick: int | None,
    mask: int,
    in_overflow: bool,
) -> str:
    """Render the wrap-safe remaining ticks until a timer expires.

    Current-list items carry the absolute expiry tick in the same epoch as
    the tick counter; an item whose expiry already passed but is still
    linked means the daemon has not processed it yet, which renders as
    ``overdue`` instead of a huge unsigned wrap value.  Overflow-list items
    belong to the next tick epoch: their ``xItemValue`` wrapped, so the
    real expiry is ``2**bits + expiry`` (timers.c prvInsertTimerInActiveList).
    The arithmetic lives in :func:`core.formatting.timer_expires_in`; this name is
    kept as the adapter-facing seam its unit tests and tables call.
    """
    return derive_timer_expires_in(expiry, tick, mask, in_overflow=in_overflow)


def daemon_is_current(layout: FreeRtosLayout) -> bool:
    """Whether the timer daemon task is the currently running task.

    The daemon is the only writer of the timer lists and the command queue;
    while it is on a core the snapshot is an intermediate state, so callers
    print a warning on top of the table/detail.
    """
    daemon = safe_dereference(lookup_symbol("xTimerTaskHandle"))
    if daemon is None:
        return False
    daemon_addr = value_address(daemon)
    return any(tcb_addr == daemon_addr for _core, tcb_addr in current_tasks(layout))


# Message attached when the daemon runs while the snapshot is taken.
DAEMON_CURRENT_MESSAGE = (
    "the timer daemon task is the current task: command queue and timer "
    "lists may be mid-update"
)


def timer_name_at(address: int | None, layout: FreeRtosLayout) -> str | None:
    """Return the ``pcTimerName`` of the timer at *address*, or ``None``.

    The daemon queue stores ``Timer_t`` pointers; the Timer cell names the
    object by its real name (what xTimerCreate was given) instead of an
    opaque variable, falling back to the raw address when the read fails
    (e.g. a command for a deleted timer).
    """
    if gdb is None or not address:
        return None
    sl = layout.structs.get("struct tmrTimerControl")
    if sl is None:
        return None
    value = value_at(address, sl)
    if value is None:
        return None
    return read_cstring(read_field(value, sl, "name"))


def _message_value(address: int, msg_layout):
    """Cast a ring-slot address to a ``DaemonTaskMessage_t`` value."""
    return value_at(address, msg_layout)


def _callback_arm_present(msg_type) -> bool:
    """Whether the queue message union carries ``xCallbackParameters``.

    The member only exists under ``INCLUDE_xTimerPendFunctionCall == 1``
    (timers.c); probing the DWARF union instead of guessing the macro keeps
    this honest when the config is enabled through a header GDB cannot see.
    When it is absent a negative ``xMessageID`` cannot be a real pended
    callback (the kernel cannot enqueue one, timers.c), so the slot is
    reported odd rather than decoded as a callback.
    """
    try:
        union = msg_type["u"]
        return "xCallbackParameters" in type_field_names(union.type)
    except (KeyError, TypeError, AttributeError):
        return False


def iter_timer_commands(
    layout: FreeRtosLayout,
) -> tuple[list[str], list[TimerCommand]]:
    """Ring-read the daemon command queue, returning ``(messages, commands)``.

    ``xTimerQueue`` is a ``QueueHandle_t`` (a pointer), so it is
    dereferenced before any field read; the payload slot walk reuses the
    queue FIFO walker and each slot address is cast back to
    ``DaemonTaskMessage_t`` so field decoding goes through DWARF instead of
    hand-rolled byte parsing.  ``messages`` carries the reason when the
    queue cannot be inspected (wrong item size, unreadable queue) or states
    that nothing is pending -- a caller renders it instead of fabricating
    an empty table.
    """
    if not layout.config.timers:
        return ["no software timers in this build (configUSE_TIMERS=0)"], []
    timer_queue = safe_dereference(lookup_symbol("xTimerQueue"))
    if timer_queue is None:
        return ["timer command queue xTimerQueue is unavailable"], []
    msg_layout = layout.internal_structs.get("struct tmrTimerQueueMessage")
    msg_type = lookup_type(msg_layout.struct_name) if msg_layout is not None else None
    if msg_type is None:
        return ["DaemonTaskMessage_t is not in DWARF; cannot decode commands"], []
    ql = layout.structs["struct QueueDefinition"]
    item_size = read_int(read_field(timer_queue, ql, "item_size"))
    expected = type_size(msg_type)
    if expected is None:
        return ["timer command queue message size is unreadable"], []
    if item_size is None:
        return ["timer command queue item size is unreadable"], []
    if item_size != expected:
        return [
            f"skipped: item size {item_size} != sizeof(DaemonTaskMessage_t) {expected}"
        ], []
    waiting = read_int(read_field(timer_queue, ql, "count"))
    if waiting is None:
        return ["timer command queue uxMessagesWaiting is unreadable"], []
    if waiting == 0:
        return ["no pending timer commands"], []
    commands: list[TimerCommand] = []
    for seq, address, _payload in iter_queue_items(timer_queue, layout):
        message_value = _message_value(address, msg_layout)
        if message_value is None:
            commands.append(TimerCommand(seq=seq, message_id=None))
            continue
        msg_layout = layout.internal_structs["struct tmrTimerQueueMessage"]
        message_id = read_int(read_field(message_value, msg_layout, "message_id"))
        if message_id is None:
            commands.append(TimerCommand(seq=seq, message_id=None))
            continue
        if message_id < 0:
            arm = _callback_arm_present(msg_type)
            callback = TimerCommand(
                seq=seq,
                message_id=message_id,
                callback_arm=arm,
            )
            # NOTE: the acceptance for a genuinely pended callback is the
            # real callback parameters (timers.c u.xCallbackParameters) -- a
            # bare "pended callback" label would hide the values the daemon
            # would invoke.  The union arms are declared in the layout, so a
            # build without the member degrades each read to None (same as
            # the DWARF arm probe).
            if arm:
                callback = TimerCommand(
                    seq=seq,
                    message_id=message_id,
                    callback_arm=True,
                    callback_function=read_int(
                        read_field(message_value, msg_layout, "callback_fn")
                    ),
                    callback_parameter1=read_int(
                        read_field(message_value, msg_layout, "callback_p1")
                    ),
                    callback_parameter2=read_int(
                        read_field(message_value, msg_layout, "callback_p2")
                    ),
                )
            commands.append(callback)
            continue
        timer = read_int(read_field(message_value, msg_layout, "timer"))
        message_value_field = read_int(
            read_field(message_value, msg_layout, "message_value")
        )
        commands.append(
            TimerCommand(
                seq=seq,
                message_id=message_id,
                timer_address=timer,
                message_value=message_value_field,
            )
        )
    if waiting and not commands:
        # NOTE: uxMessagesWaiting > 0 but the FIFO walker yielded nothing
        # (unreadable head/tail pointers or a degenerate span) -- rendering
        # "no pending timer commands" would mask a corrupt queue as an
        # empty one, so the walk failure is reported instead.
        return [
            f"timer command queue slots unreadable ({waiting} message(s) waiting)"
        ], []
    return [], commands


def _callback_value(cmd: TimerCommand) -> str:
    """Render the Value cell of a pended-callback command row.

    With the ``u.xCallbackParameters`` arm present the daemon would invoke
    ``pxCallbackFunction(pvParameter1, ulParameter2)`` -- those three
    values are the only honest cell.  Without the arm a negative
    ``xMessageID`` cannot be a real pended callback (the kernel cannot
    enqueue one, timers.c), so the reason is stated instead of guessing.
    """
    if not cmd.callback_arm:
        return "pended-callback arm absent (INCLUDE_xTimerPendFunctionCall=0)"
    fn = cmd.callback_function
    p1 = cmd.callback_parameter1
    p2 = cmd.callback_parameter2
    if fn is None and p1 is None and p2 is None:
        return "pended callback"
    fn_cell = format_symbol_or_address(fn, lookup_symbol_at(fn)) if fn else "-"
    parts = [fn_cell]
    if p1 is not None:
        parts.append(f"p1={p1:#x}")
    if p2 is not None:
        parts.append(f"p2={p2:#x}")
    return " ".join(parts)


def commands_cell(
    messages: list[str],
    commands: list[TimerCommand],
    layout: FreeRtosLayout,
) -> str:
    """Render the pending-command section of the timer detail.

    ``messages`` carries the honest reason when the queue cannot be read
    (or the "no pending" statement); with a decodable queue the commands
    render as a small Seq/Command/Timer/Value table.
    """
    if messages:
        return "\n".join(messages)
    rows: list[list[str]] = []
    for cmd in commands:
        if cmd.message_id is None:
            rows.append([str(cmd.seq), "unreadable", "-", "-"])
        elif cmd.message_id < 0:
            rows.append(
                [
                    str(cmd.seq),
                    command_name(cmd.message_id),
                    "-",
                    _callback_value(cmd),
                ]
            )
        else:
            rows.append(
                [
                    str(cmd.seq),
                    command_name(cmd.message_id),
                    timer_name_at(cmd.timer_address, layout)
                    or format_address(cmd.timer_address),
                    format_optional_int(cmd.message_value),
                ]
            )
    if not rows:
        return "no pending timer commands"
    return format_table(
        rows, ["Seq", "Command", "Timer", "Value"], elastic=("Timer", "Value")
    ).rstrip()


# ---------------------------------------------------------------------------
# Event-group decode.
# ---------------------------------------------------------------------------
def event_bit_masks(tick_bits: int) -> tuple[int, int, int, int]:
    """Return the four control-bit masks for a tick width W in ``{16,32,64}``.

    ``EventBits_t`` is ``TickType_t`` (event_groups.h), so the control bits
    live in the top byte: ``CLEAR_ON_EXIT=1<<(W-8)``,
    ``UNBLOCKED_DUE_TO_BIT_SET=2<<(W-8)``, ``WAIT_FOR_ALL=4<<(W-8)`` and
    ``CONTROL_BYTES=0xff<<(W-8)`` (event_groups.h).  These are preprocessor
    constants with no DWARF representation, so they must be derived -- on a
    16-bit tick a hard-coded ``0x01000000`` would fall outside EventBits_t
    and every ``wants``/``missing`` decode would silently keep the control
    bits.
    """
    shift = tick_bits - 8
    return (1 << shift, 2 << shift, 4 << shift, 0xFF << shift)


@dataclass
class EventWaiter:
    """One decoded waiter on an event group's ``xTasksWaitingForBits`` list.

    ``raw`` is the list item's ``xItemValue``: application bits OR'd with the
    control bits AND ``eventIN_USE`` (event_groups.c vTaskPlaceOnUnordered-
    EventList ORs ``eventIN_USE = 1<<(W-1)`` onto the request, tasks.c).  The
    decode order strips ``IN_USE`` and ``CONTROL_BYTES`` first, leaving
    ``wants``; a waiter whose ``in_use`` bit is clear is a suspicious value
    and is rendered with a marker instead of silently treated as valid.
    """

    task: str = "-"
    raw: int = 0
    wants: int = 0
    wait_all: bool = False
    clear_on_exit: bool = False
    satisfied: bool = False
    missing: int = 0
    in_use: bool = False


def decode_waiter(
    raw: int | None,
    current_bits: int | None,
    masks: tuple[int, int, int, int],
) -> EventWaiter | None:
    """Decode one list-item value into an :class:`EventWaiter`, ``None`` on an
    unreadable value.

    The satisfied/missing computation follows event_groups.c
    ``prvTestWaitCondition``: ALL mode needs every wanted bit set
    (``(cur & wants) == wants``, missing is what is still absent), ANY mode
    needs a single wanted bit (``(cur & wants) != 0``, missing stays the
    full request because any one bit would unblock it).
    """
    if raw is None:
        return None
    clear_on_exit, _unblocked, wait_for_all, control_bytes = masks
    # NOTE: eventIN_USE = 1 << (W-1) is the top bit, and the top byte
    # (CONTROL_BYTES) spans it; deriving it from the control mask keeps the
    # decode width-agnostic without threading tick_bits through every call.
    in_use_mask = 1 << (control_bytes.bit_length() - 1)
    wants = raw & ~(control_bytes | in_use_mask)
    wait_all = bool(raw & wait_for_all)
    if current_bits is None:
        return EventWaiter(
            task="-",
            raw=raw,
            wants=wants,
            wait_all=wait_all,
            clear_on_exit=bool(raw & clear_on_exit),
            satisfied=False,
            missing=wants if wait_all else wants,
            in_use=bool(raw & in_use_mask),
        )
    if wait_all:
        satisfied = (current_bits & wants) == wants
        missing = wants & ~current_bits
    else:
        satisfied = (current_bits & wants) != 0
        missing = wants
    return EventWaiter(
        task="-",
        raw=raw,
        wants=wants,
        wait_all=wait_all,
        clear_on_exit=bool(raw & clear_on_exit),
        satisfied=satisfied,
        missing=missing,
        in_use=bool(raw & in_use_mask),
    )


def format_waiter_line(waiter: EventWaiter) -> str:
    """Render one waiter as ``wants=0x.. mode=ALL|ANY clearOnExit=yes|no
    missing=0x..``, appending ``(satisfied — mid-unblock)`` when the bits are
    already satisfied.

    The mid-unblock suffix is only possible while the item is still linked:
    the kernel ORs ``eventUNBLOCKED_DUE_TO_BIT_SET`` into the *return* value
    and removes the item from the list at the same time (event_groups.c), so
    a satisfied waiter still on the list means ``xEventGroupSetBits`` has not
    run yet -- exactly the transient the marker names.
    """
    mode = "ALL" if waiter.wait_all else "ANY"
    clear = "yes" if waiter.clear_on_exit else "no"
    line = (
        f"wants=0x{waiter.wants:x} mode={mode} clearOnExit={clear} "
        f"missing=0x{waiter.missing:x}"
    )
    if waiter.satisfied:
        line += " (satisfied — mid-unblock)"
    if not waiter.in_use:
        line += " (IN_USE clear — suspicious item value)"
    return line


@dataclass
class FreeRtosEventGroupObject:
    """Display model for one event group (struct EventGroupDef_t).

    ``statically_allocated`` is only populated when the build has both static
    and dynamic allocation (the ``ucStaticallyAllocated`` member only exists
    then); otherwise static-ness must come from the discovery channel.
    """

    name: str = "-"
    address: int = 0
    bits: int | None = None
    waiters: list[EventWaiter] = None  # type: ignore[assignment]
    source: str = ""
    extra_sources: tuple[str, ...] = ()
    statically_allocated: bool | None = None

    def __post_init__(self) -> None:
        if self.waiters is None:
            self.waiters = []


def _event_control_tick_bits(layout: FreeRtosLayout) -> int:
    """Return the bit width used for EventBits_t control/IN_USE masks.

    Pre-10.5.0 kernels (without ``configTICK_TYPE_WIDTH_IN_BITS``) hardcode
    32-bit control and ``taskEVENT_LIST_ITEM_VALUE_IN_USE`` masks even when
    a 64-bit port typedefs ``TickType_t`` to 64 bits.
    """
    tick_bits = layout.config.tick_bits
    if tick_bits > 32 and layout.version is not None and layout.version < (10, 5, 0):
        return 32
    return tick_bits


def iter_waiters(
    value,
    layout: FreeRtosLayout,
    current_bits: int | None,
):
    """Yield decoded :class:`EventWaiter` entries from ``xTasksWaitingForBits``.

    The event-group wait list is an *unordered* list of ``xLIST_ITEM`` nodes
    whose owner is the blocked task's TCB; the request bits live in the *list
    item's* ``xItemValue`` (event_groups.c), so decoding must read the item,
    never recompute from the TCB.
    """
    head = read_field(value, layout.structs["struct EventGroupDef_t"], "waiting")
    if head is None:
        return
    masks = event_bit_masks(_event_control_tick_bits(layout))
    try:
        list_layout = layout.structs["struct xLIST"]
        end = read_field(head, list_layout, "end")
        end_addr = value_address(end)
        mini_layout = layout.structs["struct xMINI_LIST_ITEM"]
        node = read_field(end, mini_layout, "next")
        item_layout = layout.structs["struct xLIST_ITEM"]
        seen: set[int] = set()
        ranges = mapped_ranges()
        for _ in range(GDR_MAX_TRAVERSAL_COUNT):
            node_addr = safe_int(node)
            if not node_addr or node_addr == end_addr:
                return
            # NOTE: same corruption guard as navigation._iter_list -- a
            # next pointer outside every loadable section is not a list node;
            # stop before dereference (skipped when no map is available so
            # unit tests with synthetic addresses still exercise the walk).
            if ranges and not any(low <= node_addr < high for low, high in ranges):
                warn(
                    f"FreeRTOS waiter list traversal stopped at out-of-range "
                    f"node {node_addr:#x}"
                )
                return
            if node_addr in seen:
                warn(
                    f"FreeRTOS waiter list traversal stopped at repeated "
                    f"node {node_addr:#x}"
                )
                return
            seen.add(node_addr)
            item = safe_dereference(node)
            if item is None:
                return
            raw = read_int(read_field(item, item_layout, "value"))
            owner = read_int(read_field(item, item_layout, "owner"))
            waiter = decode_waiter(raw, current_bits, masks)
            if waiter is not None:
                name = task_name_at(owner, layout) if owner else None
                waiter.task = name or "-"
                yield waiter
            node = read_field(item, item_layout, "next")
    except TARGET_ACCESS_ERRORS:
        return


def value_to_event_group_object(
    value,
    found,
    layout: FreeRtosLayout,
) -> FreeRtosEventGroupObject:
    """Convert a discovered event-group address into the display model."""
    obj = FreeRtosEventGroupObject(
        name=found.name or "-",
        address=found.address,
        source=found.source,
        extra_sources=found.extra_sources,
    )
    if value is None:
        return obj
    bits = read_int(
        read_field(value, layout.structs["struct EventGroupDef_t"], "value")
    )
    obj.bits = bits
    obj.waiters = list(iter_waiters(value, layout, bits))
    if layout.config.static_and_dynamic:
        static = read_int(
            read_field(value, layout.structs["struct EventGroupDef_t"], "static_alloc")
        )
        obj.statically_allocated = bool(static) if static is not None else None
    return obj


def bits_cell(bits: int | None) -> str:
    """Render the Bits cell: hex, ``-`` when unreadable."""
    return "-" if bits is None else f"0x{bits:x}"


_EVENT_GROUP_HEADERS = [spec.key for spec in EVENT_GROUP_SPECS]


def event_group_table(
    objects: list[FreeRtosEventGroupObject],
    layout: FreeRtosLayout,
) -> ObjectTable:
    """Build the ``frt eventgroups`` table with the verbatim header contract.

    A build without event groups answers with an explicit capability note
    instead of an empty table that reads as "zero event groups".
    """
    messages: list[str] = []
    if not layout.config.event_groups:
        messages.append("no event groups in this build (configUSE_EVENT_GROUPS=0)")
    rows = [
        [
            text_cell(obj.name),
            raw_cell(bits_cell(obj.bits), obj.bits),
            number_cell(len(obj.waiters)),
            text_cell(source_label(obj.source, obj.extra_sources)),
            address_cell(obj.address),
        ]
        for obj in objects
    ]
    return ObjectTable.from_specs(
        EVENT_GROUP_SPECS, rows, messages=messages, id_key="Addr"
    )


# ---------------------------------------------------------------------------
# Stream/message/batching-buffer decode.
# ---------------------------------------------------------------------------
# stream_buffer.c sbFLAGS_* (ucFlags bit definitions).
_FLAG_MESSAGE_BUFFER = 0x01
_FLAG_STATICALLY_ALLOCATED = 0x02
_FLAG_BATCHING_BUFFER = 0x04


def bytes_in_buffer(head: int, tail: int, length: int) -> int:
    """Return the number of bytes currently stored in a stream buffer.

    Mirrors stream_buffer.c ``prvBytesInBuffer``: ``count = xLength +
    xHead - xTail``, decremented by ``xLength`` when it reaches ``xLength``
    (the implicit two's-complement wrap of the raw ring offsets).  Exactly
    equivalent to ``(xHead - xTail) % xLength`` including the
    ``xHead < xTail`` wrap.
    """
    count = length + head - tail
    if count >= length:
        count -= length
    return count


def spaces_available(head: int, tail: int, length: int) -> int:
    """Return the bytes available for a write of a *single* byte stream.

    Mirrors the space computation in stream_buffer.c
    ``xStreamBufferSpacesAvailable``: ``xLength + xTail - xHead - 1``,
    minus ``xLength`` when it overflows.  One byte is always reserved so a
    full ring stays distinguishable from an empty one; the static variant's
    ``xLength`` equals the storage size while a dynamic buffer's ``xLength``
    already includes the extra byte, which is why the *capacity* of both is
    ``xLength - 1``.
    """
    space = length + tail - head - 1
    if space >= length:
        space -= length
    return space


def trigger_met(bytes_used: int, trigger: int | None, batching: bool) -> bool:
    """Whether the byte count crosses the trigger level.

    Batching buffers notify only on a *strictly greater* count
    (``>``), every other buffer on ``>=`` (stream_buffer.c
    ``prvBytesInBufferMeetTriggerLevel``).  The asymmetry matters exactly at
    ``bytes == trigger``: a batching buffer at the level still blocks its
    receiver.
    """
    if trigger is None:
        return False
    return bytes_used > trigger if batching else bytes_used >= trigger


def stream_kind(flags: int | None) -> str:
    """Classify a buffer by ``ucFlags``: ``stream``/``message``/``batching``.

    The static bit is deliberately left out of the kind: it stays a property
    of the object (Src / detail), never of the table's Type column, so the
    column contract does not drift between static-only and dynamic variants.
    """
    if flags is None:
        return "unknown"
    if flags & _FLAG_BATCHING_BUFFER:
        return "batching"
    if flags & _FLAG_MESSAGE_BUFFER:
        return "message"
    return "stream"


def stream_label(flags: int | None) -> str:
    """Render the full six-value type label, e.g. ``message+static``.

    ``ucFlags`` can be 0/1/2/3/4/6 (stream_buffer.c): the message bit (1),
    the static-allocation bit (2) and, from V11.1.0, the batching bit (4)
    combine freely.  This full label belongs in the per-object detail, where
    it cannot destabilize a variant-wide table assertion.
    """
    kind = stream_kind(flags)
    if flags is None or not flags & _FLAG_STATICALLY_ALLOCATED:
        return kind
    return f"{kind}+static"


def message_length_size() -> tuple[int, bool]:
    """Return ``(bytes, assumed)`` for ``sbBYTES_TO_STORE_MESSAGE_LENGTH``.

    The macro is ``sizeof(configMESSAGE_BUFFER_LENGTH_TYPE)`` which defaults
    to ``size_t`` (FreeRTOS.h), and the macro has no DWARF representation and
    the only kernel function carrying the typedef is usually not linked into
    the fixture -- so the value is derived from ``sizeof(size_t)`` and marked
    *assumed* so readers never mistake it for a probed fact.  The size_t path
    failing (no DWARF) degrades to a 4-byte assumption for the target byte
    order, still flagged assumed.
    """
    mb_size = type_size("configMESSAGE_BUFFER_LENGTH_TYPE")
    if mb_size is not None:
        return mb_size, False
    size_t_size = type_size("size_t")
    if size_t_size is not None:
        return size_t_size, True
    return 4, True


@dataclass
class FreeRtosStreamBufferObject:
    """Display model for one stream/message/batching buffer.

    ``kind`` is the base classification (stream|message|batching); the
    static-allocation bit is reflected through the discovery channel and the
    detail's ``StaticallyAllocated`` row, never through ``kind``.  ``deleted``
    is the post-``vStreamBufferDeleteStatic`` signature (``xLength == 0`` and
    ``pucBuffer == NULL``, stream_buffer.c); a deleted buffer must short-
    circuit every geometry read -- dividing by ``xLength`` would raise.
    """

    name: str = "-"
    address: int = 0
    kind: str = "stream"
    flags: int | None = None
    deleted: bool = False
    length: int | None = None
    capacity: int | None = None
    bytes_used: int | None = None
    space: int | None = None
    trigger: int | None = None
    trigger_met: bool | None = None
    next_message: int | None = None
    recv_waiter: str | None = None
    send_waiter: str | None = None
    notification_index: int | None = None
    message_length_bytes: int = 4
    message_length_assumed: bool = True
    source: str = ""
    extra_sources: tuple[str, ...] = ()


def value_to_stream_buffer_object(
    value,
    found,
    layout: FreeRtosLayout,
) -> FreeRtosStreamBufferObject:
    """Convert a discovered stream-buffer address into the display model.

    A deleted buffer (``xLength == 0 && pucBuffer == NULL``) stops here:
    capacity/bytes/space/trigger stay ``None`` and the detail refuses the
    deferred division instead of raising on a zero length.
    """
    obj = FreeRtosStreamBufferObject(
        name=found.name or "-",
        address=found.address,
        source=found.source,
        extra_sources=found.extra_sources,
    )
    if value is None:
        return obj
    sl = layout.structs["struct StreamBufferDef_t"]
    length = read_int(read_field(value, sl, "size"))
    buffer = read_int(read_field(value, sl, "buffer"))
    obj.length = length
    # NOTE: the deleted signature is xLength==0 *and* a readable NULL
    # pucBuffer (vStreamBufferDeleteStatic memsets the struct); an unreadable
    # pointer is not evidence of deletion and must not claim it.
    obj.deleted = length == 0 and buffer == 0
    if obj.deleted:
        return obj
    head = read_int(read_field(value, sl, "head"))
    tail = read_int(read_field(value, sl, "tail"))
    trigger = read_int(read_field(value, sl, "trigger"))
    flags = read_int(read_field(value, sl, "flags"))
    obj.flags = flags
    obj.kind = stream_kind(flags)
    obj.capacity = length - 1 if length is not None and length > 0 else None
    if head is not None and tail is not None and length is not None and length > 0:
        obj.bytes_used = bytes_in_buffer(head, tail, length)
        obj.space = spaces_available(head, tail, length)
    obj.trigger = trigger
    if obj.bytes_used is not None and trigger is not None:
        obj.trigger_met = trigger_met(
            obj.bytes_used, trigger, batching=obj.kind == "batching"
        )
    obj.message_length_bytes, obj.message_length_assumed = message_length_size()
    if obj.kind == "message" and obj.bytes_used is not None:
        # NOTE: NextMsg is the length prefix stored at ring offset xTail
        # (stream_buffer.c prvReadBytesFromBuffer reads pucBuffer[xTail]),
        # only valid while a full length field has been written
        # (prvBytesInBuffer > sbBYTES_TO_STORE_MESSAGE_LENGTH guard); the
        # prefix width is the message-length assumption, not a hard-coded 4.
        if obj.bytes_used > obj.message_length_bytes and tail is not None and buffer:
            obj.next_message = _read_prefix(buffer, tail, obj)
        else:
            obj.next_message = None
    recv = read_int(read_field(value, sl, "recv_waiter"))
    send = read_int(read_field(value, sl, "send_waiter"))
    if recv:
        obj.recv_waiter = task_name_at(recv, layout) or "-"
    if send:
        obj.send_waiter = task_name_at(send, layout) or "-"
    if layout.config.stream_buffer_notification_index:
        index = read_int(read_field(value, sl, "notification_index"))
        obj.notification_index = index
    return obj


def _read_prefix(buffer: int, tail: int, obj: FreeRtosStreamBufferObject) -> int | None:
    """Read the length-prefix word at ring offset ``xTail`` (NextMsg).

    ``xTail`` is a byte *offset* into ``pucBuffer`` (stream_buffer.c), so the
    word lives at ``pucBuffer + xTail``; like the kernel's own
    ``prvReadBytesFromBuffer`` the read splits into two segments when the
    prefix straddles the ring end.  ``None`` (rendered ``-``) when the read
    fails or the offset/width is not a sane ring geometry.
    """
    length = obj.length
    width = obj.message_length_bytes
    if not length or tail is None or tail >= length or width > length:
        return None
    raw = bytearray()
    offset = tail
    remaining = width
    while remaining > 0:
        chunk = min(remaining, length - offset)
        piece = read_bytes(buffer + offset, chunk)
        if piece is None:
            return None
        raw.extend(piece)
        remaining -= chunk
        # NOTE: prvReadBytesFromBuffer wraps the tail back to offset 0 once
        # the segment reaches xLength (stream_buffer.c).
        offset = 0
    arch = get_arch_info()
    if arch is not None and arch.endian == "big":
        return int.from_bytes(raw, "big")
    return int.from_bytes(raw, "little")


def bounds_check(obj: FreeRtosStreamBufferObject, value, layout: FreeRtosLayout) -> str:
    """Three-state ring-bounds verdict: ``ok`` / ``skipped: ...`` / ``fail: ...``.

    xHead/xTail are byte offsets into the storage window and must stay below
    xLength, and a non-deleted buffer must own a non-NULL pucBuffer
    (stream_buffer.c prvInitialiseNewStreamBuffer).  A deleted buffer is
    structurally not a ring anymore and reports ``skipped`` -- an inapplicable
    check is never disguised as a failure.
    """
    if obj.deleted:
        return "skipped: deleted buffer"
    length = obj.length
    sl = layout.structs["struct StreamBufferDef_t"]
    head = read_int(read_field(value, sl, "head"))
    tail = read_int(read_field(value, sl, "tail"))
    buffer = read_int(read_field(value, sl, "buffer"))
    if length is None or head is None or tail is None or buffer is None:
        return "skipped: unreadable"
    if not buffer:
        return "fail: pucBuffer is NULL on a live buffer"
    if head >= length or tail >= length:
        return "fail: xHead/xTail outside [0, xLength)"
    return "ok"


_STREAM_BUFFER_HEADERS = [
    "Name",
    "Type",
    "Bytes",
    "Space",
    "Capacity",
    "Trigger",
    "NextMsg",
    "RecvWait",
    "SendWait",
    "Src",
    "Addr",
]


def stream_buffer_table(
    objects: list[FreeRtosStreamBufferObject],
    layout: FreeRtosLayout,
) -> ObjectTable:
    """Build the ``frt streambuffers`` table with the verbatim contract.

    Messages state why the table cannot lie: an explicit capability note for
    builds without stream buffers, and (always) the fact that stream buffers
    store no waiter lists, so the waiter channel does not apply and an
    unregistered dynamic buffer with no global handle is never enumerable.
    """
    messages: list[str] = []
    if not layout.config.stream_buffers:
        messages.append("no stream buffers in this build (configUSE_STREAM_BUFFERS=0)")
    messages.append(
        "stream buffers keep single TaskHandle_t waiters, not wait lists: "
        "no waiter discovery channel exists, and unregistered dynamic "
        "buffers with no global handle are not enumerable"
    )
    rows = []
    for obj in objects:
        next_msg: str
        if obj.kind == "message":
            next_msg = "-" if obj.next_message is None else f"0x{obj.next_message:x}"
        else:
            next_msg = "N/A"
        rows.append(
            [
                text_cell(obj.name),
                text_cell(obj.kind),
                raw_cell(
                    "-" if obj.bytes_used is None else str(obj.bytes_used),
                    obj.bytes_used,
                ),
                raw_cell("-" if obj.space is None else str(obj.space), obj.space),
                raw_cell(
                    "-" if obj.capacity is None else str(obj.capacity), obj.capacity
                ),
                raw_cell("-" if obj.trigger is None else str(obj.trigger), obj.trigger),
                raw_cell(next_msg, obj.next_message),
                text_cell(obj.recv_waiter or "-"),
                text_cell(obj.send_waiter or "-"),
                text_cell(source_label(obj.source, obj.extra_sources)),
                address_cell(obj.address),
            ]
        )
    return ObjectTable.from_specs(
        STREAM_BUFFER_SPECS, rows, messages=messages, id_key="Addr"
    )


def resolve_discovered_object(
    kind: str,
    target: str,
    layout: FreeRtosLayout,
    *,
    discover_fn,
    resolve_object_fn,
    default_resolve_fn,
):
    """Resolve one non-task FreeRTOS object via discovery, symbols, and addresses."""
    from core.object_ref import ObjectCandidate, ObjectResolution, resolve_object_ref
    from freertos.navigation import DiscoveredObject, symbol_object_addresses

    candidates = [
        ObjectCandidate(obj.address, obj, obj.name) for obj in discover_fn(kind, layout)
    ]
    if resolve_object_fn is not default_resolve_fn:
        res = resolve_object_ref(kind, target, candidates)
        if res.status != "not_found":
            return res
        resolved = resolve_object_fn(kind, target, layout)
        return (
            ObjectResolution("found", resolved, resolved.address)
            if resolved is not None
            else ObjectResolution("not_found")
        )
    return resolve_object_ref(
        kind,
        target,
        candidates,
        symbol_addresses=lambda sym: symbol_object_addresses(kind, sym, layout),
        synthesize_address=lambda addr, name: DiscoveredObject(
            kind, addr, name, "user"
        ),
        allow_numeric_symbol=True,
    )
