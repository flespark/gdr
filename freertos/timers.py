"""Timer daemon inspection: active-list epochs, owner checks and command queue.

The timer service task ("Tmr Svc", timers.c) owns two ``List_t`` heads
(xActiveTimerList1/2, referenced through the swappable
``pxCurrentTimerList``/``pxOverflowTimerList`` pointers) plus a command
queue (``xTimerQueue``).  This module decodes all three for the ``frt
timers`` table and the ``frt timer <name>`` detail: which epoch a linked
timer lives in, whether the list item's ``pvOwner`` matches the object, and
what commands are still pending in the daemon queue.

Every GDB entry point goes through module-level helpers (``read_path``,
``lookup_symbol``, ``safe_dereference``, ...) so the unit tests can drive
the logic without a GDB session, following ``freertos/navigation.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from freertos.layout import FreeRtosLayout
from freertos.navigation import current_tasks
from gdr.formatting import (
    format_address,
    format_optional_int,
    format_symbol_or_address,
    format_table,
)
from gdr.gdb_bridge import (
    lookup_symbol,
    lookup_symbol_at,
    lookup_type,
    read_cstring,
    read_int,
    safe_dereference,
    value_address,
)
from gdr.layout import read_path

if gdb is not None:
    _TIMER_ERRORS: tuple[type[BaseException], ...] = (
        gdb.error,
        gdb.MemoryError,
        IndexError,
        TypeError,
        ValueError,
        AttributeError,
    )
else:
    _TIMER_ERRORS = (IndexError, TypeError, ValueError, AttributeError)

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


def state_cell(source: str, status: int | None) -> str:
    """Render the State cell: ``active``/``dormant``/``unknown``.

    The daemon updates ``ucStatus`` and the lists together while processing
    commands (timers.c prvProcessReceivedCommands); a queued START or STOP
    leaves them disagreeing.  A ``?`` suffix marks that in-flight
    transition instead of guessing which side wins.
    """
    if status is None:
        return "unknown"
    active = bool(status & _TMR_STATUS_ACTIVE)
    linked = source == "active"
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
    """
    if expiry is None or tick is None:
        return "N/A"
    if in_overflow:
        return str(((mask + 1) - tick) + expiry)
    if expiry < tick:
        return "overdue"
    return str(expiry - tick)


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
    try:
        sl = layout.structs["struct tmrTimerControl"]
        typ = gdb.lookup_type(sl.struct_name).pointer()
        value = gdb.Value(address).cast(typ).dereference()
        return read_cstring(read_path(value, ("pcTimerName",)))
    except _TIMER_ERRORS:
        return None


def _message_value(address: int, msg_type):
    """Cast a ring-slot address to a ``DaemonTaskMessage_t`` value."""
    if gdb is None or not address:
        return None
    try:
        return gdb.Value(address).cast(msg_type.pointer()).dereference()
    except _TIMER_ERRORS:
        return None


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
        return any(f.name == "xCallbackParameters" for f in union.type.fields())
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
    msg_type = lookup_type("struct tmrTimerQueueMessage")
    if msg_type is None:
        return ["DaemonTaskMessage_t is not in DWARF; cannot decode commands"], []
    item_size = read_int(read_path(timer_queue, ("uxItemSize",)))
    try:
        expected = int(msg_type.sizeof)
    except (TypeError, ValueError, AttributeError):
        return ["timer command queue message size is unreadable"], []
    if item_size is None:
        return ["timer command queue item size is unreadable"], []
    if item_size != expected:
        return [
            f"skipped: item size {item_size} != sizeof(DaemonTaskMessage_t) {expected}"
        ], []
    waiting = read_int(read_path(timer_queue, ("uxMessagesWaiting",)))
    if waiting is None:
        return ["timer command queue uxMessagesWaiting is unreadable"], []
    if waiting == 0:
        return ["no pending timer commands"], []
    commands: list[TimerCommand] = []
    # Reason: local import -- this module is imported by freertos.details
    # (which renders the command section), so importing the queue FIFO
    # walker at module scope would form a cycle; by call time the module
    # graph is fully loaded.
    from freertos.details import iter_queue_items

    for seq, address, _payload in iter_queue_items(timer_queue, layout):
        message_value = _message_value(address, msg_type)
        if message_value is None:
            commands.append(TimerCommand(seq=seq, message_id=None))
            continue
        message_id = read_int(read_path(message_value, ("xMessageID",)))
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
            # Reason: the acceptance for a genuinely pended callback is the
            # real callback parameters (timers.c u.xCallbackParameters) -- a
            # bare "pended callback" label would hide the values the daemon
            # would invoke.  Only attempt the union read when the arm exists.
            if arm:
                params = read_path(message_value, ("u", "xCallbackParameters"))
                if params is not None:
                    callback = TimerCommand(
                        seq=seq,
                        message_id=message_id,
                        callback_arm=True,
                        callback_function=read_int(
                            read_path(params, ("pxCallbackFunction",))
                        ),
                        callback_parameter1=read_int(
                            read_path(params, ("pvParameter1",))
                        ),
                        callback_parameter2=read_int(
                            read_path(params, ("ulParameter2",))
                        ),
                    )
            commands.append(callback)
            continue
        timer = read_int(read_path(message_value, ("u", "xTimerParameters", "pxTimer")))
        message_value_field = read_int(
            read_path(message_value, ("u", "xTimerParameters", "xMessageValue"))
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
        # Reason: uxMessagesWaiting > 0 but the FIFO walker yielded nothing
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
