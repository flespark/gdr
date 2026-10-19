"""RT-Thread's RTOS-specific command tree."""

from __future__ import annotations

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

from core.adapter import active
from core.commands import (
    CommandTreeSpec,
    WatchSpec,
    add_watch,
    complete_command_tree,
    complete_watch,
    remove_watch,
    render_heap,
    render_heap_dump,
    render_heap_dump_options,
    render_object_detail,
    render_objects,
    render_system,
    render_tasks,
    render_watch_list,
)
from core.commands import (
    clear as clear_watches,
)
from core.gdb_bridge import (
    gdb_command_guard,
    info,
    warn,
)
from core.help import (
    CommandHelp,
    HelpField,
    HelpTopic,
    HelpTree,
    command_aliases,
    command_topics,
    find_topic,
    functions_topic,
    pretty_printer_topic,
    render_terminal,
)
from core.layout import KernelLayout
from rtthread.adapter import RtThreadAdapter
from rtthread.diagnostics import render_check
from rtthread.navigation import iter_object_names


# ---------------------------------------------------------------------------
# Structured command documentation.
# ---------------------------------------------------------------------------
def _fields(*items: tuple[str, str]) -> tuple[HelpField, ...]:
    return tuple(HelpField(*item) for item in items)


_ADDR = ("Addr", "Target address of the kernel object.")
_THREAD_ADDR = ("Addr", "Thread control block address; the row identity.")
_WAITERS = ("Waiters", "Blocked tasks as `count@names`; N/A means no list exists.")
_SNAPSHOT_LIMIT = (
    "The target stays halted and GDR never calls inferior code; values are one "
    "bounded snapshot and may capture an in-progress kernel transition."
)


_HEAP_DUMP = HelpTopic(
    name="dump",
    summary="Draw the heap occupancy bitmap",
    description=(
        "Draws every heap block as a coloured cell: grey for free, one colour per "
        "owning thread (MEMTRACE), a neutral blue when owners are unknown. Inline in "
        "iTerm2 or Kitty; otherwise written to a PNG file."
    ),
    usage=("rtt heap dump [path] [--cell-pixel N] [--width W] [--row-bytes N]",),
    fields=_fields(
        (
            "Cells",
            "The arena split evenly into a fixed grid, row by row; a block paints every cell it touches.",
        ),
        (
            "Owner colours",
            "Assigned by allocated bytes, largest first; the summary lists owner totals.",
        ),
        (
            "Cell size",
            "--cell-pixel N draws N-pixel cells (1-64, default 10).",
        ),
        (
            "Width",
            "--width W caps the bitmap pixel width (default 800); it must fit at least one cell.",
        ),
        (
            "Row bytes",
            "--row-bytes N tiles every row with exactly N arena bytes (no folding); too small a value errors past 4096 rows.",
        ),
        (
            "File",
            "Default gdr-heap-rtthread-gen<N>.png in the working directory, or the given path.",
        ),
    ),
    tips=(
        "GDR_HEAP_IMAGE=iterm|kitty|file overrides terminal detection.",
        "The panel's heap card draws the same bitmap with a hover readout.",
    ),
    configuration=(
        "Inline output needs TERM_PROGRAM=iTerm.app or KITTY_WINDOW_ID; VS Code's debug console shows text only and falls back to a file.",
    ),
    limitations=(_SNAPSHOT_LIMIT,),
)


def _threads_list() -> CommandHelp:
    return CommandHelp(
        "threads",
        "List registered threads",
        "Shows scheduling, priority, stack, entry-point and optional SMP state for every registered thread.",
        usage=("rtt threads",),
        fields=_fields(
            ("Name", "Thread name; '*' marks a currently running thread."),
            ("State", "Decoded scheduler state."),
            ("Prio/BasePrio", "Current and initial priority."),
            (
                "PC",
                "Last PC as <symbol+offset>, the running PC or the return address on the saved switch frame; a source link when the line resolves. N/A when the frame cannot be proven.",
            ),
            ("SP", "Current stack pointer."),
            (
                "Stack/Used/HighWater",
                "Stack capacity, current use and maximum observed use, in bytes.",
            ),
            ("Entry", "Symbolized entry function or address."),
            ("CPU/Bind", "Current/bound CPU when SMP fields exist."),
            _THREAD_ADDR,
        ),
        tips=(
            "Use `rtt thread <name-or-address>` for one thread's Key: Value details.",
            'Use `p $gdr_task("<name-or-address>")` for native rt_thread inspection.',
        ),
        configuration=("CPU and Bind appear only in an RT_USING_SMP layout.",),
        limitations=(_SNAPSHOT_LIMIT,),
        aliases=("tasks",),
        action="tasks",
        kind="task",
    )


def _thread_detail() -> CommandHelp:
    return CommandHelp(
        "thread",
        "Show one thread in detail",
        "Prints one thread as Key: Value pairs, including identity, scheduling, stack, entry, error/tick state and CPU placement.",
        usage=("rtt thread <name-or-address>",),
        fields=_fields(
            ("Name/Address/Type", "Thread identity and native object type."),
            (
                "State/Priority/BasePriority",
                "Decoded state and current/initial priorities.",
            ),
            (
                "SP/Stack/StackSize/Used/HighWater",
                "Stack geometry and watermark-derived usage; sizes in bytes.",
            ),
            ("Entry", "Symbolized entry function or address."),
            ("Error/RemainingTick", "Kernel error and remaining time slice."),
            ("BindCPU/OnCPU", "SMP placement when present."),
        ),
        tips=('Inspect additional fields with `p $gdr_task("<name-or-address>")`.',),
        configuration=(
            "SMP rows require RT_USING_SMP; watermark quality depends on stack fill bytes.",
        ),
        limitations=(_SNAPSHOT_LIMIT,),
        action="detail",
        kind="task",
    )


def _objects_summary() -> CommandHelp:
    return CommandHelp(
        "objects",
        "Summarise registry-backed object counts",
        "Counts enabled object kinds by traversing RT-Thread's object registry.",
        usage=("rtt objects",),
        fields=_fields(
            ("Kind", "Semantic object kind."),
            ("Count", "Objects found in the registry."),
            ("Sources", "Registry provenance for the count."),
        ),
        tips=("Open the matching plural command to inspect individual rows.",),
        configuration=(
            "Only object classes enabled in the detected kernel configuration are listed.",
        ),
        limitations=(
            "Detached or corrupted registry nodes cannot be counted.",
            _SNAPSHOT_LIMIT,
        ),
        action="objects",
    )


def _check_command() -> CommandHelp:
    return CommandHelp(
        "check",
        "Run explicit consistency checks",
        "Reports each pass, failure and skip with the object address and concrete evidence. Normal views show only problems.",
        usage=(
            "rtt check [system|heap|thread|semaphore|mutex|event|mailbox|messagequeue|mempool|timer] [target]",
        ),
        fields=_fields(
            ("Object", "Object name and address or system/heap scope."),
            (
                "Check/Verdict/Evidence",
                "Invariant, ok/fail/skipped and observed fields/locations.",
            ),
        ),
        tips=(
            "Run after kernel initialization; stops inside kernel updates can be transient.",
        ),
        configuration=(
            "Unavailable fields report skipped with the missing symbol, field or config macro.",
        ),
        limitations=(_SNAPSHOT_LIMIT,),
        action="check",
    )


def _watch_command() -> CommandHelp:
    return CommandHelp(
        "watch",
        "Print watched objects on every stop",
        "Prints a watched kind's list table, or one object's Key: Value detail, each time the target stops. Like GDB display, not a watchpoint: it never stops the target itself.",
        usage=(
            "rtt watch",
            "rtt watch <kind>",
            "rtt watch <object> <name-or-address>",
        ),
        fields=_fields(
            ("Kind", "A plural list command (threads, semaphores, timers, ...)."),
            ("Target", "A singular detail selector: name, address or symbol."),
            ("Output", "One [gdr watch: ...] block per watch, in the order added."),
        ),
        tips=(
            "A singleton must exist when added; a later detach renders as gone instead of a stale address.",
        ),
        configuration=("Watchable kinds follow the enabled kernel components.",),
        limitations=(
            "Long tables and `set pagination on` can hold the prompt at every stop; keep watches narrow.",
            "Watches are session state and are dropped by `gdr reset`.",
            _SNAPSHOT_LIMIT,
        ),
        action="watch",
    )


def _unwatch_command() -> CommandHelp:
    return CommandHelp(
        "unwatch",
        "Drop stop-time watches",
        "Removes one watch, or every watch with --all.",
        usage=(
            "rtt unwatch <kind> | <object> <target>",
            "rtt unwatch --all",
        ),
        tips=("Run bare `rtt watch` to list the set with its labels.",),
        limitations=(_SNAPSHOT_LIMIT,),
        action="unwatch",
    )


def _system_summary() -> CommandHelp:
    return CommandHelp(
        "system",
        "Show kernel, scheduler, object and heap summary",
        "Combines current thread, tick/state counts, object counts and system-heap status.",
        usage=("rtt system",),
        fields=_fields(
            ("Current task", "Current thread or per-core current threads."),
            (
                "Task/Tick/Scheduler",
                "Registry count, tick count and scheduler state: Unlocked, Locked (nest N), or In ISR (nest N). The row is omitted when no lock or interrupt counter is readable.",
            ),
            ("Object counts", "Enabled registry-backed object totals."),
            (
                "Heap allocator/used/total/status",
                "Detected system allocator snapshot.",
            ),
        ),
        tips=("Use `rtt heap` when heap status is not good or counters are N/A.",),
        configuration=("Heap and object sections follow detected symbols/components.",),
        limitations=(
            "Missing symbols render N/A rather than being guessed.",
            _SNAPSHOT_LIMIT,
        ),
        action="system",
    )


def _heap_command() -> CommandHelp:
    return CommandHelp(
        "heap",
        "Inspect the RT-Thread system heap",
        "Reports allocator counters and a bounded block walk. Owner-level occupancy is drawn by `rtt heap dump`.",
        usage=("rtt heap", "rtt heap dump [path]"),
        children=(_HEAP_DUMP,),
        fields=_fields(
            ("Algorithm", "Detected small_mem, memheap, slab, or none allocator."),
            (
                "UsedSize/TotalSize/MaxUsed",
                "Kernel heap counters in bytes when exported.",
            ),
            ("Status", "good, corrupt, overrun or capability state."),
            ("Blocks", "Used/free/total block counts from a bounded walk."),
        ),
        tips=(
            "A corrupt/overrun status should be investigated before resuming the target.",
            "Use hole sizes to distinguish fragmentation from total exhaustion.",
        ),
        configuration=(
            "Shape depends on the detected small_mem, memheap or slab implementation.",
            "Heap-dump owner colours require RT_USING_MEMTRACE owner fields.",
        ),
        limitations=(
            "Owner attribution is N/A without MEMTRACE and may be truncated by the kernel's short owner-name field.",
            "Unreadable bounds or block headers keep counters but suppress unsafe walk results.",
            _SNAPSHOT_LIMIT,
        ),
        action="heap",
    )


def build_help_tree(layout: KernelLayout | None = None) -> HelpTree:
    """Build the RT-Thread help tree, optionally with live printer metadata."""
    commands = (
        _threads_list(),
        _thread_detail(),
        _semaphore_list(),
        _semaphore_detail(),
        _mutex_list(),
        _mutex_detail(),
        _event_list(),
        _event_detail(),
        _mailbox_list(),
        _mailbox_detail(),
        _messagequeue_list(),
        _messagequeue_detail(),
        _mempool_list(),
        _mempool_detail(),
        _timer_list(),
        _timer_detail(),
        _objects_summary(),
        _check_command(),
        _watch_command(),
        _unwatch_command(),
        _system_summary(),
        _heap_command(),
    )
    return HelpTree(
        program="rtt",
        title="RT-Thread help",
        summary="Reference for RT-Thread commands, output fields and GDR inspection helpers.",
        topics=(*commands, functions_topic(), pretty_printer_topic(layout)),
    )


def _ipc_configuration(component: str) -> tuple[str, ...]:
    return (f"Requires the detected RT_USING_{component} component.",)


def _semaphore_list() -> CommandHelp:
    return CommandHelp(
        "semaphores",
        "List semaphores",
        "Lists available counts, scheduling policy and blocked takers.",
        usage=("rtt semaphores",),
        fields=_fields(
            ("Name/Value", "Semaphore identity and available count."),
            ("Policy", "FIFO or priority waiter ordering."),
            _WAITERS,
            _ADDR,
        ),
        tips=("A zero Value with waiters indicates contention.",),
        configuration=_ipc_configuration("SEMAPHORE"),
        limitations=(_SNAPSHOT_LIMIT,),
        aliases=("sems",),
        action="objects",
        kind="semaphore",
    )


def _semaphore_detail() -> CommandHelp:
    return CommandHelp(
        "semaphore",
        "Show one semaphore in detail",
        "Shows count, policy, waiter names and waiter-list diagnostics.",
        usage=("rtt semaphore <name-or-address>",),
        fields=_fields(
            ("Name/Address/Type", "Object identity."),
            ("Value/Policy", "Available count and scheduling policy."),
            _WAITERS,
        ),
        tips=("Continue with `rtt thread <waiter>` to inspect a blocked task.",),
        configuration=_ipc_configuration("SEMAPHORE"),
        limitations=(_SNAPSHOT_LIMIT,),
        action="detail",
        kind="semaphore",
    )


def _mutex_list() -> CommandHelp:
    return CommandHelp(
        "mutexes",
        "List mutexes",
        "Lists ownership, recursive hold depth, inheritance priority and waiters.",
        usage=("rtt mutexes",),
        fields=_fields(
            ("Name/Value", "Mutex identity and available state."),
            ("Hold/Owner", "Recursive hold count and owner thread."),
            ("OrigPrio", "Owner priority before inheritance."),
            ("Policy", "Waiter scheduling policy."),
            _WAITERS,
            _ADDR,
        ),
        tips=(
            "Compare OrigPrio with the owner's current priority to diagnose inheritance.",
        ),
        configuration=_ipc_configuration("MUTEX"),
        limitations=(_SNAPSHOT_LIMIT,),
        aliases=("mtxs",),
        action="objects",
        kind="mutex",
    )


def _mutex_detail() -> CommandHelp:
    return CommandHelp(
        "mutex",
        "Show one mutex in detail",
        "Shows ownership, recursive depth, inherited-priority baseline, policy and waiters.",
        usage=("rtt mutex <name-or-address>",),
        fields=_fields(
            ("Value/Hold/Owner", "Availability, recursive depth and holder."),
            ("OriginalPriority", "Holder priority before inheritance."),
            ("Policy/Waiters", "Wait ordering and blocked threads."),
        ),
        tips=("Use `rtt thread <owner>` to compare current and base priority.",),
        configuration=_ipc_configuration("MUTEX"),
        limitations=(_SNAPSHOT_LIMIT,),
        action="detail",
        kind="mutex",
    )


def _event_list() -> CommandHelp:
    return CommandHelp(
        "events",
        "List event objects",
        "Lists current bit sets, policy and blocked event waiters.",
        usage=("rtt events",),
        fields=_fields(
            ("Name/Set", "Object name and current event mask."),
            ("Policy", "FIFO or priority waiter ordering."),
            _WAITERS,
            _ADDR,
        ),
        tips=("Use detail help to decode each waiter's requested mask and mode.",),
        configuration=_ipc_configuration("EVENT"),
        limitations=(_SNAPSHOT_LIMIT,),
        action="objects",
        kind="event",
    )


def _event_detail() -> CommandHelp:
    return CommandHelp(
        "event",
        "Show one event object in detail",
        "Shows current bits plus every waiter's requested mask, AND/OR mode and clear behavior.",
        usage=("rtt event <name-or-address>",),
        fields=_fields(
            ("Set", "Current event bit mask."),
            ("Waiters", "Waiter summary."),
            ("Waiter[n]", "Thread event_set/event_info condition decode."),
        ),
        tips=("A condition already satisfied can be a transient mid-wakeup state.",),
        configuration=_ipc_configuration("EVENT"),
        limitations=(_SNAPSHOT_LIMIT,),
        action="detail",
        kind="event",
    )


def _mailbox_list() -> CommandHelp:
    return CommandHelp(
        "mailboxs",
        "List mailboxes",
        "Lists ring occupancy, capacity, offsets, policy and sender/receiver waiters.",
        usage=("rtt mailboxs",),
        fields=_fields(
            ("Name/Entry/Size/Free", "Identity and slot occupancy/capacity."),
            ("In/Out", "Ring write/read offsets."),
            ("Policy", "Waiter ordering."),
            ("RecvWait/SendWait", "Blocked receivers and senders."),
            _ADDR,
        ),
        tips=(
            "Use `rtt mailbox <name-or-address>` to dump occupied slots and validate offsets.",
        ),
        configuration=_ipc_configuration("MAILBOX"),
        limitations=(_SNAPSHOT_LIMIT,),
        aliases=("mboxs", "mailboxes"),
        action="objects",
        kind="mailbox",
    )


def _mailbox_detail() -> CommandHelp:
    return CommandHelp(
        "mailbox",
        "Show one mailbox in detail",
        "Shows ring pointers/offsets, FIFO slots, waiters and consistency diagnostics.",
        usage=("rtt mailbox <name-or-address>",),
        fields=_fields(
            ("Entry/Size/InOffset/OutOffset", "Ring occupancy and positions."),
            ("OffsetCheck/SlotCheck", "Bounds and readability verdicts."),
            ("MsgPool/Slot[n]", "Storage base and pointer-sized FIFO values."),
            ("RecvWait/SendWait", "Blocked task summaries."),
        ),
        tips=("OffsetCheck identifies metadata corruption before slot traversal.",),
        configuration=_ipc_configuration("MAILBOX"),
        limitations=(
            "Slots are rendered as pointer-sized raw values, not application message types.",
            _SNAPSHOT_LIMIT,
        ),
        action="detail",
        kind="mailbox",
    )


def _messagequeue_list() -> CommandHelp:
    return CommandHelp(
        "messagequeues",
        "List message queues",
        "Lists fixed-size message occupancy, capacity, policy and blocked tasks.",
        usage=("rtt messagequeues",),
        fields=_fields(
            ("Name/Entry", "Identity and queued messages."),
            ("MsgSize/MaxMsgs/Free", "Element size, capacity and free slots."),
            ("Policy", "Waiter ordering."),
            ("RecvWait/SendWait", "Blocked receivers/senders."),
            _ADDR,
        ),
        tips=(
            "Use detail output to compare cached entry count with active/free node walks.",
        ),
        configuration=_ipc_configuration("MESSAGEQUEUE"),
        limitations=(
            "Older layouts have no sender wait list; SendWait is N/A, not zero.",
            _SNAPSHOT_LIMIT,
        ),
        aliases=("msgs",),
        action="objects",
        kind="msgqueue",
    )


def _messagequeue_detail() -> CommandHelp:
    return CommandHelp(
        "messagequeue",
        "Show one message queue in detail",
        "Walks active/free message nodes and compares them with cached queue counters.",
        usage=("rtt messagequeue <name-or-address>",),
        fields=_fields(
            ("Entry/MsgSize/MaxMsgs", "Queue geometry."),
            ("MsgPool/Msg[n]", "Pool base and bounded payload prefixes."),
            ("ActiveNodes/FreeNodes/Consistency", "Node counts and cross-check."),
            ("RecvWait/SendWait", "Blocked task summaries."),
        ),
        tips=("A mismatch identifies which cached or walked count disagrees.",),
        configuration=_ipc_configuration("MESSAGEQUEUE"),
        limitations=(
            "Payloads are bounded hexadecimal prefixes without application type decoding.",
            _SNAPSHOT_LIMIT,
        ),
        action="detail",
        kind="msgqueue",
    )


def _mempool_list() -> CommandHelp:
    return CommandHelp(
        "mempools",
        "List fixed-block memory pools",
        "Lists block geometry, free/used counts and blocked allocators.",
        usage=("rtt mempools",),
        fields=_fields(
            ("Name/BlockSize", "Pool identity and block size in bytes."),
            ("Total/Free/Used", "Block counts."),
            _WAITERS,
            _ADDR,
        ),
        tips=("Use detail output to validate free-list count and alignment.",),
        configuration=_ipc_configuration("MEMPOOL"),
        limitations=(_SNAPSHOT_LIMIT,),
        action="objects",
        kind="mempool",
    )


def _mempool_detail() -> CommandHelp:
    return CommandHelp(
        "mempool",
        "Show one fixed-block memory pool in detail",
        "Shows pool bounds, free-list head, waiters and count/alignment checks.",
        usage=("rtt mempool <name-or-address>",),
        fields=_fields(
            ("BlockSize/Total/Free", "Block geometry and cached counts."),
            ("StartAddress/PoolSize/BlockList", "Arena and free-list pointers."),
            (
                "AlignmentCheck/FreeCountCheck",
                "Geometry and walked-vs-cached verdicts.",
            ),
            _WAITERS,
        ),
        tips=("A FreeCountCheck mismatch is direct free-list corruption evidence.",),
        configuration=_ipc_configuration("MEMPOOL"),
        limitations=(_SNAPSHOT_LIMIT,),
        action="detail",
        kind="mempool",
    )


def _timer_list() -> CommandHelp:
    return CommandHelp(
        "timers",
        "List kernel timers",
        "Lists active state, reload mode, deadline, callback and wrap-safe time remaining.",
        usage=("rtt timers",),
        fields=_fields(
            ("Name/State", "Timer identity and active state."),
            (
                "Mode/TimerType",
                "Periodic/one-shot reload mode and soft/hard execution context.",
            ),
            ("InitTick/TimeoutTick/ExpiresIn", "Period/deadline and wrap-safe delta."),
            ("Callback", "Symbolized callback or address."),
            _ADDR,
        ),
        tips=(
            "Compare TimeoutTick to the Kernel tick message when investigating expiry.",
        ),
        configuration=("Soft-timer behavior depends on RT_USING_TIMER_SOFT.",),
        limitations=(_SNAPSHOT_LIMIT,),
        action="objects",
        kind="timer",
    )


def _timer_detail() -> CommandHelp:
    return CommandHelp(
        "timer",
        "Show one kernel timer in detail",
        "Shows state, periodicity, timer class, deadline, callback, parameter and expiry delta.",
        usage=("rtt timer <name-or-address>",),
        fields=_fields(
            ("State/Mode/TimerType", "Activity, reload mode and soft/hard class."),
            (
                "InitTick/TimeoutTick/ExpiresIn",
                "Configured interval, deadline and wrap-safe delta.",
            ),
            ("Callback/Parameter", "Symbolized callback and argument."),
        ),
        tips=("Inactive timers intentionally show ExpiresIn as N/A.",),
        configuration=(
            "Timer class follows target flag bits and soft-timer configuration.",
        ),
        limitations=(_SNAPSHOT_LIMIT,),
        action="detail",
        kind="timer",
    )


_command_registered = False
_alias_registered = False

_HELP_TREE = build_help_tree()
_COMMANDS = {topic.name: topic for topic in command_topics(_HELP_TREE)}
_COMMAND_ALIASES = command_aliases(_HELP_TREE)
_SINGULAR_COMMANDS = {
    topic.name: topic.kind for topic in _COMMANDS.values() if topic.action == "detail"
}
# Compatibility views for callers that inspect the public command vocabulary.
_COMMAND_DESCRIPTIONS = {
    "help": "Show this help",
    **{
        name: topic.summary
        for name, topic in _COMMANDS.items()
        if topic.action != "detail"
    },
}
_DETAIL_DESCRIPTIONS = {
    name: topic.summary for name, topic in _COMMANDS.items() if topic.action == "detail"
}
_USAGE = "usage: rtthread <command> (run 'rtt help <topic>' for detailed help)"
_HELP = render_terminal(_HELP_TREE)
_WATCH_KINDS = {
    name: ("task" if topic.action == "tasks" else topic.kind)
    for name, topic in _COMMANDS.items()
    if topic.action == "tasks" or (topic.action == "objects" and topic.kind)
}
_WATCH_USAGE = (
    "usage: rtthread watch [<kind> | <object> <target>] "
    "(run 'rtt help watch' for the kind list)"
)
_UNWATCH_USAGE = "usage: rtthread unwatch [<kind> | <object> <target> | --all]"


def _help_tree():
    """Return docs enriched with the active adapter's concrete printer layout."""
    adapter = active()
    layout = adapter.layout if isinstance(adapter, RtThreadAdapter) else None
    return build_help_tree(layout)


@gdb_command_guard
def _invoke_command(argument: str) -> None:
    """Parse and dispatch one RT-Thread command without depending on GDB."""
    args = argument.split()
    if not args:
        print(render_terminal(_help_tree()))
        return
    if args[0].lower() == "help":
        path = tuple(args[1:])
        tree = _help_tree()
        if path and find_topic(tree, path) is None:
            warn(_USAGE)
            return
        print(render_terminal(tree, path))
        return
    command = _COMMAND_ALIASES.get(args[0].lower(), args[0].lower())
    if command == "check":
        render_check(args[1:])
    elif command == "watch":
        _invoke_watch(args[1:])
    elif command == "unwatch":
        _invoke_unwatch(args[1:])
    elif command == "heap" and len(args) >= 2 and args[1].lower() == "dump":
        options, error = render_heap_dump_options(args[2:], _USAGE)
        if error is not None:
            warn(error)
        else:
            render_heap_dump(
                "rtthread",
                options.path,
                cell_px=options.cell_px,
                max_px=options.max_px,
                row_bytes=options.row_bytes,
            )
    elif len(args) == 2 and command in _SINGULAR_COMMANDS:
        render_object_detail(_SINGULAR_COMMANDS[command], args[1])
    elif len(args) != 1:
        warn(_USAGE)
    elif command in _COMMANDS and _COMMANDS[command].action == "tasks":
        render_tasks()
    elif command in _COMMANDS and _COMMANDS[command].action == "objects":
        render_objects(_COMMANDS[command].kind)
    elif command in _COMMANDS and _COMMANDS[command].action == "system":
        render_system()
    elif command in _COMMANDS and _COMMANDS[command].action == "heap":
        render_heap()
    else:
        warn(_USAGE)


def _command_vocabulary() -> list[str]:
    """Return every word the first argument may complete against."""
    return ["help", *list(_COMMANDS), "functions", "pretty-printers"]


def _watch_spec() -> WatchSpec:
    """Watchable words derived from the tree's own routing metadata."""
    return WatchSpec(
        kind_words=dict(_WATCH_KINDS),
        singular_kinds=dict(_SINGULAR_COMMANDS),
        aliases=dict(_COMMAND_ALIASES),
        object_names_fn=_object_names,
    )


def _invoke_watch(words: list[str]) -> None:
    """Add a stop-time watch, or list the set when given no words."""
    if not words:
        render_watch_list()
        return
    first = _COMMAND_ALIASES.get(words[0].lower(), words[0].lower())
    if first in _WATCH_KINDS:
        if len(words) != 1:
            warn(_WATCH_USAGE)
            return
        refusal = add_watch(f"rtt {words[0].lower()}", _WATCH_KINDS[first], None)
    elif first in _SINGULAR_COMMANDS:
        if len(words) != 2:
            warn(_WATCH_USAGE)
            return
        kind = _SINGULAR_COMMANDS[first]
        refusal = add_watch(f"rtt {words[0].lower()} {words[1]}", kind, words[1])
    else:
        warn(_WATCH_USAGE)
        return
    if refusal is not None:
        warn(refusal)
    else:
        info(
            f"watching {words[0].lower()}" + (f" {words[1]}" if len(words) > 1 else "")
        )


def _invoke_unwatch(words: list[str]) -> None:
    """Drop one watch, or every watch with ``--all``."""
    if not words:
        warn(_UNWATCH_USAGE)
        return
    if len(words) == 1 and words[0] == "--all":
        clear_watches()
        info("all watches cleared")
        return
    first = _COMMAND_ALIASES.get(words[0].lower(), words[0].lower())
    if first in _WATCH_KINDS and len(words) == 1:
        removed = remove_watch(_WATCH_KINDS[first], None)
        label = words[0].lower()
    elif first in _SINGULAR_COMMANDS and len(words) == 2:
        removed = remove_watch(_SINGULAR_COMMANDS[first], words[1])
        label = f"{words[0].lower()} {words[1]}"
    else:
        warn(_UNWATCH_USAGE)
        return
    if removed:
        info(f"unwatched {label}")
    else:
        warn(f"no such watch: {label}")


def _object_names(kind: str) -> list[str]:
    """Return live object names of *kind* for tab completion.

    Traverses the active adapter's kernel registry so candidates reflect the
    objects on the connected target right now. Degrades to ``[]`` when no
    RT-Thread adapter is active or traversal fails, so tab completion never
    raises inside GDB.
    """
    adapter = active()
    if not isinstance(adapter, RtThreadAdapter):
        return []
    try:
        return list(iter_object_names(kind, adapter.layout))
    except Exception:
        # NOTE: this is the GDB completion boundary (``complete()``), not a
        # command body. GDB completion must never raise or print -- the guard's
        # warn/err output would corrupt the readline prompt -- so we swallow
        # everything here and degrade to no candidates, as documented on
        # :func:`_object_names`.
        return []


def _complete(text: str, word: str | None) -> list[str]:
    """Return tab-completion candidates for a partial ``rtt`` command line.

    The first argument completes against the command vocabulary; the second
    argument of a singular detail command completes against live kernel
    object names.  Words after ``watch``/``unwatch`` complete against the
    watchable kinds and the same live names.  Mechanics are shared with the
    FreeRTOS tree through :func:`core.commands.complete_command_tree`.
    """
    parts = text.split()
    if parts and _COMMAND_ALIASES.get(parts[0].lower(), parts[0].lower()) in (
        "watch",
        "unwatch",
    ):
        return complete_watch(_watch_spec(), text, word)
    return complete_command_tree(
        CommandTreeSpec(
            _command_vocabulary(),
            _COMMAND_ALIASES,
            _SINGULAR_COMMANDS,
            _object_names,
            help_topics=[topic.name for topic in _HELP_TREE.topics],
        ),
        text,
        word,
    )


if gdb is not None:  # pragma: no cover - gdb.Command exists only in GDB

    class RtThreadCommand(gdb.Command):
        """RT-Thread command tree. Run `rtt help` for available commands."""

        def __init__(self) -> None:
            # NOTE: no completer_class: GDB calls ``complete()`` only then;
            # passing one would silently disable subcommand completion.
            super().__init__("rtthread", gdb.COMMAND_USER)

        def invoke(self, argument: str, from_tty: bool) -> None:  # noqa: ARG002
            _invoke_command(argument)

        def complete(self, text: str, word: str | None) -> list[str]:
            """Tab-complete subcommands and live object names.

            Completing the second argument of a singular detail command walks
            the active adapter's kernel registry, so candidates reflect the
            objects that exist on the connected target right now.
            """
            return _complete(text, word)


def register_commands() -> None:
    """Register the command tree and alias, resuming after partial failure."""
    global _alias_registered, _command_registered
    if _command_registered and _alias_registered:
        return
    if gdb is None:
        raise RuntimeError("not running inside GDB")
    if not _command_registered:
        RtThreadCommand()
        _command_registered = True
    if not _alias_registered:
        gdb.execute("alias rtt = rtthread")
        _alias_registered = True
    info("rtthread commands registered (alias: rtt)")
