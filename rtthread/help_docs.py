"""Structured RT-Thread command documentation."""

from __future__ import annotations

from gdr.help import (
    CommandHelp,
    HelpField,
    HelpTree,
    functions_topic,
    pretty_printer_topic,
)
from gdr.layout import KernelLayout


def _fields(*items: tuple[str, str]) -> tuple[HelpField, ...]:
    return tuple(HelpField(*item) for item in items)


_ADDR = ("Addr", "Target address of the kernel object.")
_WAITERS = ("Waiters", "Blocked tasks as `count@names`; N/A means no list exists.")
_SNAPSHOT_LIMIT = (
    "The target stays halted and GDR never calls inferior code; values are one "
    "bounded snapshot and may capture an in-progress kernel transition."
)


def build_help_tree(layout: KernelLayout | None = None) -> HelpTree:
    """Build the RT-Thread help tree, optionally with live printer metadata."""
    commands = (
        CommandHelp(
            "threads",
            "List registered threads",
            "Shows scheduling, priority, stack, entry-point and optional SMP state for every registered thread.",
            usage=("rtt threads",),
            fields=_fields(
                ("Name", "Thread name; '*' marks a currently running thread."),
                ("State", "Decoded scheduler state."),
                ("Prio/BasePrio", "Current and initial priority."),
                ("SP", "Current stack pointer."),
                (
                    "Stack/Used/HighWater",
                    "Stack capacity, current use and maximum observed use.",
                ),
                ("Entry", "Symbolized entry function or address."),
                ("CPU/Bind", "Current/bound CPU when SMP fields exist."),
                _ADDR,
            ),
            tips=(
                "Use `rtt thread <name>` for error, remaining tick and SMP details.",
                'Use `p $gdr_task("<name>")` for native rt_thread inspection.',
            ),
            configuration=("CPU and Bind appear only in an RT_USING_SMP layout.",),
            limitations=(_SNAPSHOT_LIMIT,),
            aliases=("tasks",),
            action="tasks",
            kind="task",
        ),
        CommandHelp(
            "thread",
            "Show one thread in detail",
            "Displays identity, scheduling, stack, entry function, error/tick state and CPU placement for one thread.",
            usage=("rtt thread <name>",),
            fields=_fields(
                ("Name/Address/Type", "Thread identity and native object type."),
                (
                    "State/Priority/BasePriority",
                    "Decoded state and current/initial priorities.",
                ),
                (
                    "SP/Stack/StackSize/Used/HighWater",
                    "Stack geometry and watermark-derived usage.",
                ),
                ("Entry", "Symbolized entry function or address."),
                ("Error/RemainingTick", "Kernel error and remaining time slice."),
                ("BindCPU/OnCPU", "SMP placement when present."),
            ),
            tips=('Inspect additional fields with `p $gdr_task("<name>")`.',),
            configuration=(
                "SMP rows require RT_USING_SMP; watermark quality depends on stack fill bytes.",
            ),
            limitations=(_SNAPSHOT_LIMIT,),
            action="detail",
            kind="task",
        ),
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
        CommandHelp(
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
        ),
        CommandHelp(
            "system",
            "Show kernel, scheduler, object and heap summary",
            "Combines kernel version, current thread, tick/state counts, object counts and system-heap status.",
            usage=("rtt system",),
            fields=_fields(
                ("Kernel version", "Declared RT-Thread version."),
                ("Current task", "Current thread or per-core current threads."),
                (
                    "Task/Tick/Scheduler",
                    "Registry count, tick count and scheduler state.",
                ),
                ("Object counts", "Enabled registry-backed object totals."),
                (
                    "Heap allocator/used/total/status",
                    "Detected system allocator snapshot.",
                ),
            ),
            tips=("Use `rtt heap` when heap status is not good or counters are N/A.",),
            configuration=(
                "Heap and object sections follow detected symbols/components.",
            ),
            limitations=(
                "Missing symbols render N/A rather than being guessed.",
                _SNAPSHOT_LIMIT,
            ),
            action="system",
        ),
        CommandHelp(
            "heap",
            "Inspect the RT-Thread system heap",
            "Reports allocator counters, a bounded block walk, fragmentation and optional MEMTRACE owner occupancy.",
            usage=("rtt heap",),
            fields=_fields(
                ("Algorithm", "Detected small_mem, memheap, slab, or none allocator."),
                ("Used/Total/Maximum", "Kernel heap counters when exported."),
                ("Status", "good, corrupt, overrun or capability state."),
                ("Blocks", "Used/free/total block counts from a bounded walk."),
                ("Holes", "Free-hole count and size summary."),
                (
                    "Thread occupancy",
                    "MEMTRACE owner count and optional Thread/Blocks/Bytes table.",
                ),
            ),
            tips=(
                "A corrupt/overrun status should be investigated before resuming the target.",
                "Use hole sizes to distinguish fragmentation from total exhaustion.",
            ),
            configuration=(
                "Shape depends on the detected small_mem, memheap or slab implementation.",
                "Per-thread occupancy requires RT_USING_MEMTRACE owner fields.",
            ),
            limitations=(
                "Owner attribution is N/A without MEMTRACE and may be truncated by the kernel's short owner-name field.",
                "Unreadable bounds or block headers keep counters but suppress unsafe walk results.",
                _SNAPSHOT_LIMIT,
            ),
            action="heap",
        ),
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
        usage=("rtt semaphore <name>",),
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
        usage=("rtt mutex <name>",),
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
        usage=("rtt event <name>",),
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
        tips=("Use `rtt mailbox <name>` to dump occupied slots and validate offsets.",),
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
        usage=("rtt mailbox <name>",),
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
        usage=("rtt messagequeue <name>",),
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
            ("Name/BlockSize", "Pool identity and block bytes."),
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
        usage=("rtt mempool <name>",),
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
        "Lists active state, mode/type, deadline, callback and wrap-safe time remaining.",
        usage=("rtt timers",),
        fields=_fields(
            ("Name/State", "Timer identity and active state."),
            ("Mode/Type", "Periodic/one-shot and soft/hard classification."),
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
        usage=("rtt timer <name>",),
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
