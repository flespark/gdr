"""Structured FreeRTOS command documentation."""

from __future__ import annotations

from freertos.layout import FreeRtosLayout
from gdr.help import (
    CommandHelp,
    HelpField,
    HelpTree,
    functions_topic,
    pretty_printer_topic,
)


def _fields(*items: tuple[str, str]) -> tuple[HelpField, ...]:
    return tuple(HelpField(*item) for item in items)


_DISCOVERY_LIMIT = (
    "FreeRTOS has no complete global object registry. GDR merges MPU-pool, registry, "
    "active-list, global-symbol, waiter and explicit-user discovery channels; "
    "unregistered dynamic objects with no reachable handle remain invisible."
)
_CHECK_LIMIT = (
    "Checks inspect one halted snapshot. A task currently modifying the object can "
    "produce a transient result; no inferior function is called to stabilise it."
)
_SOURCE_FIELD = ("Src", "Discovery evidence; '+' joins corroborating channels.")
_ADDR_FIELD = ("Addr", "Target address of the kernel object.")


def build_help_tree(layout: FreeRtosLayout | None = None) -> HelpTree:
    """Build the FreeRTOS help tree, optionally with live printer metadata."""
    commands = (
        CommandHelp(
            "tasks",
            "List scheduler tasks",
            "Shows every task reachable from the scheduler lists in one diagnostic table.",
            usage=("frt tasks",),
            fields=_fields(
                ("Name", "Task name; '*' marks a task running on a core."),
                ("State", "Ready, Blocked, Suspended, Deleted, or Running state."),
                ("Prio", "Current effective priority."),
                ("BasePrio", "Base priority before mutex inheritance, when present."),
                ("SP", "Current top-of-stack pointer."),
                ("Stack", "Configured stack bytes when pxEndOfStack is available."),
                ("Used", "Currently used stack bytes."),
                ("HighWater", "Bytes never overwritten by the 0xa5 fill pattern."),
                ("Runtime", "Raw run-time counter when statistics are enabled."),
                ("CPU", "Current SMP core, when configured."),
                ("Affinity", "SMP core-affinity mask, when present."),
                _ADDR_FIELD,
            ),
            tips=(
                "Use `frt task <name>` for blocking, notification and consistency details.",
                'Use `p $gdr_task("<name>")` for native TCB field inspection.',
            ),
            configuration=(
                "BasePrio, stack bounds, Runtime, CPU and Affinity appear only when their TCB fields/configuration exist.",
                "HighWater appears only when at least one stack has a usable fill watermark.",
            ),
            limitations=(
                "There is no Entry column: a FreeRTOS TCB does not retain the task entry function after creation.",
            ),
            aliases=("threads",),
            action="tasks",
            kind="task",
        ),
        CommandHelp(
            "task",
            "Show one task in detail",
            "Displays scheduler, stack, blocking, notification, run-time and consistency data for one task.",
            usage=("frt task <name>",),
            fields=_fields(
                ("Name/Address/Type", "Identity and Idle/Normal classification."),
                (
                    "State/Priority/BasePriority",
                    "Scheduler state and effective/base priorities.",
                ),
                (
                    "SP/Stack/StackSize/Used/HighWater",
                    "Stack geometry and watermark data.",
                ),
                ("WakeTick/BlockedOn", "Blocking deadline and host when available."),
                ("Notify[n]", "Notification value and state for each configured slot."),
                ("Runtime/Runtime%", "Raw and percentage run time when enabled."),
                ("Checks", "List and TCB consistency verdicts."),
            ),
            tips=("Names may contain spaces, for example `frt task Tmr Svc`.",),
            configuration=(
                "Optional rows follow actual DWARF TCB fields and configTASK_NOTIFICATION_ARRAY_ENTRIES.",
            ),
            limitations=(
                "BlockedOn is N/A when no discovery channel can identify the wait host.",
                _CHECK_LIMIT,
            ),
            action="detail",
            kind="task",
        ),
        _queue_list_help(),
        _queue_detail_help(),
        _semaphore_list_help(),
        _semaphore_detail_help(),
        _mutex_list_help(),
        _mutex_detail_help(),
        _timer_list_help(),
        _timer_detail_help(),
        _event_list_help(),
        _event_detail_help(),
        _stream_list_help(),
        _stream_detail_help(),
        CommandHelp(
            "objects",
            "Summarise discovered objects and provenance",
            "Counts each discovered object once under its source of record and shows source-channel evidence by kind.",
            usage=("frt objects",),
            fields=_fields(
                ("Kind", "Semantic object kind."),
                ("Count", "Number reachable in this halted snapshot."),
                ("Sources", "Per-source-of-record count breakdown."),
            ),
            tips=(
                "Use the corresponding plural command to inspect names and corroborating sources.",
            ),
            configuration=(
                "Available channels depend on registry, timers, MPU and object configuration.",
            ),
            limitations=(_DISCOVERY_LIMIT,),
            action="objects",
        ),
        CommandHelp(
            "system",
            "Show scheduler, object and heap summary",
            "Combines kernel identity, scheduler counts, object discovery, heap status and consistency checks.",
            usage=("frt system",),
            fields=_fields(
                (
                    "Kernel version",
                    "Version selected and, where possible, checked against target symbols.",
                ),
                ("Current task", "Current task or per-core running tasks."),
                ("Task/Tick/Scheduler", "Scheduler counters and state."),
                ("Object counts", "Counts from the discovery model."),
                (
                    "Heap allocator/used/total/status",
                    "Allocator snapshot when one is linked.",
                ),
                ("Checks", "Adapter-owned scheduler and object consistency verdicts."),
            ),
            tips=("Use `frt heap` or an object command for the underlying evidence.",),
            configuration=(
                "Sections appear only when the matching kernel subsystem and symbols exist.",
            ),
            limitations=(
                "A section may be N/A when debug symbols or safe traversal bounds are unavailable.",
            ),
            action="system",
        ),
        CommandHelp(
            "heap",
            "Inspect the FreeRTOS allocator",
            "Reports a stable allocator summary and, when safe, a bounded linear or free-list block table.",
            usage=("frt heap",),
            fields=_fields(
                ("Algorithm", "heap_1..heap_5, heap_3, or none."),
                ("TotalSize/FreeSize/MinEver", "Kernel size and free-space counters."),
                ("Allocs/Frees", "Successful allocation/free counters when exported."),
                ("Protector", "Heap-protector canary state when enabled."),
                ("Blocks/Holes", "Bounded walk counts and fragmentation."),
                ("CrossCheck", "Free-list, linear-walk and kernel-counter agreement."),
                (
                    "Address/Size/State|Next",
                    "Optional physical-block or free-list rows.",
                ),
            ),
            tips=(
                "Treat a mismatch/corrupt CrossCheck as evidence to inspect before continuing the target.",
                "Compare MinEver with current FreeSize to distinguish a transient peak from current pressure.",
            ),
            configuration=(
                "The available counters and walk strategy depend on heap_1..heap_5 and configENABLE_HEAP_PROTECTOR.",
            ),
            limitations=(
                "FreeRTOS block headers have no owner field, so heap use cannot be attributed per task.",
                "heap_3 wraps libc and exposes no FreeRTOS arena to walk.",
                "heap_5 without the protector exports no reliable linear region bounds; CrossCheck is unavailable.",
            ),
            action="heap",
        ),
    )
    return HelpTree(
        program="frt",
        title="FreeRTOS help",
        summary="Reference for FreeRTOS commands, output fields and GDR inspection helpers.",
        topics=(*commands, functions_topic(), pretty_printer_topic(layout)),
    )


def _queue_list_help() -> CommandHelp:
    return CommandHelp(
        "queues",
        "List queues",
        "Lists discovered Queue_t objects classified as item queues.",
        usage=("frt queues",),
        fields=_fields(
            ("Name/Type", "Object name and exact or inferred queue type."),
            (
                "Items/Length/ItemSize/Free",
                "Current occupancy, capacity, element size and free slots.",
            ),
            ("SendWait/RecvWait", "`count@names` for blocked writers/readers."),
            ("Locks", "Deferred receive/transmit unlock counters."),
            ("Set", "Containing queue set when queue sets are enabled."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=(
            "Use `frt queue <name>` to dump queued item prefixes and consistency checks.",
        ),
        configuration=(
            "Set requires configUSE_QUEUE_SETS; exact Type requires configUSE_TRACE_FACILITY.",
        ),
        limitations=(_DISCOVERY_LIMIT,),
        aliases=("qs",),
        action="objects",
        kind="queue",
    )


def _queue_detail_help() -> CommandHelp:
    return CommandHelp(
        "queue",
        "Show one queue in detail",
        "Shows queue geometry, waiters, locks, checks and bounded FIFO item bytes.",
        usage=("frt queue <name-or-address>",),
        fields=_fields(
            ("Head/Tail/WriteTo/ReadFrom", "Queue storage ring pointers."),
            ("Items/Length/ItemSize/Free", "Occupancy geometry."),
            ("SendWait/RecvWait/Locks/Set", "Blocking and deferred-operation state."),
            ("Checks", "Queue list and storage consistency."),
            ("Item[n]", "Address and bounded payload prefix in FIFO order."),
            _SOURCE_FIELD,
        ),
        tips=(
            "An explicit symbol, hexadecimal address or decimal address can select an otherwise undiscovered object.",
        ),
        configuration=("Queue sets and exact queue type are configuration-dependent.",),
        limitations=(_DISCOVERY_LIMIT, _CHECK_LIMIT),
        action="detail",
        kind="queue",
    )


def _semaphore_list_help() -> CommandHelp:
    return CommandHelp(
        "semaphores",
        "List semaphores",
        "Lists binary/counting semaphores and tasks blocked while taking them.",
        usage=("frt semaphores",),
        fields=_fields(
            ("Name/Type", "Name and binary/counting classification."),
            ("Count/Max", "Available tokens and upper bound."),
            ("Waiters", "Tasks blocked on take."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=("A zero Count with waiters indicates active contention.",),
        configuration=(
            "Exact binary/counting Type requires configUSE_TRACE_FACILITY; otherwise classification may carry `?`.",
        ),
        limitations=(_DISCOVERY_LIMIT,),
        aliases=("sems",),
        action="objects",
        kind="semaphore",
    )


def _semaphore_detail_help() -> CommandHelp:
    return CommandHelp(
        "semaphore",
        "Show one semaphore in detail",
        "Shows token counts, take waiters, lock state and queue-structure checks.",
        usage=("frt semaphore <name-or-address>",),
        fields=_fields(
            ("Count/Max", "Available tokens and capacity."),
            ("Waiters", "Tasks blocked on take."),
            ("Locks", "Deferred queue unlock counters."),
            ("Checks", "List and queue consistency."),
            _SOURCE_FIELD,
        ),
        tips=("Use waiter names to continue with `frt task <name>`.",),
        configuration=("Kind certainty depends on configUSE_TRACE_FACILITY.",),
        limitations=(
            "Semaphore detail deliberately never reads the mutex-only union arm.",
            _DISCOVERY_LIMIT,
        ),
        action="detail",
        kind="semaphore",
    )


def _mutex_list_help() -> CommandHelp:
    return CommandHelp(
        "mutexes",
        "List mutexes",
        "Lists mutex ownership, recursion and blocked takers.",
        usage=("frt mutexes",),
        fields=_fields(
            ("Name/Type", "Name and mutex/recursive-mutex type."),
            ("Held/Owner", "Ownership state and holder task."),
            ("Recursive", "Recursive take depth."),
            ("Waiters", "Tasks blocked on take."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=(
            "Compare Owner and waiter priorities in `frt mutex <name>` when diagnosing inversion.",
        ),
        configuration=("Exact recursive kind requires configUSE_TRACE_FACILITY.",),
        limitations=(_DISCOVERY_LIMIT,),
        aliases=("mtxs",),
        action="objects",
        kind="mutex",
    )


def _mutex_detail_help() -> CommandHelp:
    return CommandHelp(
        "mutex",
        "Show one mutex in detail",
        "Shows holder priorities, recursion, waiters, locks and consistency checks.",
        usage=("frt mutex <name-or-address>",),
        fields=_fields(
            ("Held/Owner", "Ownership state and task name."),
            (
                "OwnerPriority/OwnerBasePriority",
                "Effective and base holder priorities.",
            ),
            ("RecursiveCallCount", "Recursive nesting depth."),
            ("Waiters/Locks", "Blocked takers and deferred queue state."),
            ("Checks", "Mutex/list consistency."),
            _SOURCE_FIELD,
        ),
        tips=(
            "A boosted OwnerPriority relative to OwnerBasePriority is priority inheritance evidence.",
        ),
        configuration=("OwnerBasePriority appears only when uxBasePriority exists.",),
        limitations=(_DISCOVERY_LIMIT, _CHECK_LIMIT),
        action="detail",
        kind="mutex",
    )


def _timer_list_help() -> CommandHelp:
    return CommandHelp(
        "timers",
        "List software timers",
        "Lists active-list timers first, then dormant timers retained by symbols or handles.",
        usage=("frt timers",),
        fields=_fields(
            (
                "Name/State/Mode",
                "Timer identity, active/dormant evidence and auto-reload mode.",
            ),
            (
                "Period/Expiry/ExpiresIn",
                "Period and wrap-safe deadline relative to Kernel tick.",
            ),
            ("Callback/ID", "Callback symbol/address and application ID."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=(
            "Use `frt timer <name>` to inspect active-list membership and queued daemon commands.",
        ),
        configuration=(
            "Requires configUSE_TIMERS=1; number metadata depends on configUSE_TRACE_FACILITY.",
        ),
        limitations=(
            "Stopped, expired one-shot and never-started timers require a static buffer/global handle to remain discoverable.",
        ),
        action="objects",
        kind="timer",
    )


def _timer_detail_help() -> CommandHelp:
    return CommandHelp(
        "timer",
        "Show one software timer in detail",
        "Shows timing, daemon-list membership, ownership checks and pending timer commands.",
        usage=("frt timer <name-or-address>",),
        fields=_fields(
            ("State/Mode/Period", "Current evidence, reload mode and period."),
            ("Expiry/ExpiresIn", "Deadline and wrap-safe delta; N/A when dormant."),
            ("Callback/ID/List", "Callback, application ID and current/overflow list."),
            ("OwnerCheck/Checks", "List-item ownership and structural verdicts."),
            ("Commands", "Bounded timer-daemon queue decode."),
            _SOURCE_FIELD,
        ),
        tips=(
            "If the daemon is currently running, treat the command queue as an intermediate snapshot.",
        ),
        configuration=(
            "Requires software timers; pended-callback decoding requires INCLUDE_xTimerPendFunctionCall.",
        ),
        limitations=(
            "Dormant list-item expiry is stale by definition and is intentionally not displayed.",
            _CHECK_LIMIT,
        ),
        action="detail",
        kind="timer",
    )


def _event_list_help() -> CommandHelp:
    return CommandHelp(
        "eventgroups",
        "List event groups",
        "Lists current event bits and blocked waiter counts.",
        usage=("frt eventgroups",),
        fields=_fields(
            ("Name", "Discovered event-group name."),
            ("Bits", "Current application bit mask."),
            ("Waiters", "Number of tasks waiting for bits."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=("Use `frt eventgroup <name>` to decode each wait condition.",),
        configuration=(
            "Requires configUSE_EVENT_GROUPS=1; masks derive from the target TickType_t width.",
        ),
        limitations=(_DISCOVERY_LIMIT,),
        aliases=("egs",),
        action="objects",
        kind="eventgroup",
    )


def _event_detail_help() -> CommandHelp:
    return CommandHelp(
        "eventgroup",
        "Show one event group in detail",
        "Decodes current bits and every waiter's ANY/ALL, clear-on-exit and missing-bit condition.",
        usage=("frt eventgroup <name-or-address>",),
        fields=_fields(
            ("Bits/Waiters", "Current mask and waiter count."),
            ("StaticallyAllocated", "Allocation mode when the field exists."),
            ("Waiter[n]", "Task, wanted bits, mode, clear-on-exit and missing bits."),
            ("Checks", "Wait-list/control-bit consistency."),
            _SOURCE_FIELD,
        ),
        tips=(
            "A satisfied waiter marked mid-unblock is transient rather than necessarily corrupt.",
        ),
        configuration=(
            "Control bits derive from 16/32/64-bit TickType_t; allocation row needs mixed allocation support.",
        ),
        limitations=(_DISCOVERY_LIMIT, _CHECK_LIMIT),
        action="detail",
        kind="eventgroup",
    )


def _stream_list_help() -> CommandHelp:
    return CommandHelp(
        "streambuffers",
        "List stream, message and batching buffers",
        "Lists ring geometry, trigger state and the single reader/writer wait handles.",
        usage=("frt streambuffers",),
        fields=_fields(
            ("Name/Type", "Name and stream/message/batching classification."),
            (
                "Bytes/Space/Capacity/Trigger",
                "Derived ring occupancy and trigger level.",
            ),
            ("NextMsg", "Next message length for message buffers."),
            ("RecvWait/SendWait", "Single waiting task handles."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=("Use `frt streambuffer <name>` for trigger and bounds diagnostics.",),
        configuration=(
            "Requires configUSE_STREAM_BUFFERS=1; batching is version/config dependent.",
        ),
        limitations=(
            "Buffers have single waiter handles, not lists, so there is no waiter discovery channel.",
            _DISCOVERY_LIMIT,
        ),
        aliases=("sbs",),
        action="objects",
        kind="streambuffer",
    )


def _stream_detail_help() -> CommandHelp:
    return CommandHelp(
        "streambuffer",
        "Show one stream/message buffer in detail",
        "Shows ring geometry, trigger semantics, waiters, message-prefix assumptions and bounds checks.",
        usage=("frt streambuffer <name-or-address>",),
        fields=_fields(
            ("Type/Capacity/Bytes/Space", "Classification and ring geometry."),
            ("Trigger/TriggerMet", "Trigger level and current comparison result."),
            ("NextMsg/MsgLenBytes", "Message length and assumed prefix width."),
            ("RecvWait/SendWait", "Single blocked reader/writer."),
            ("NotificationIndex", "Notification slot or version-gated N/A."),
            ("BoundsCheck", "Ring offsets/pointer validation."),
            _SOURCE_FIELD,
        ),
        tips=(
            "A deleted static buffer is reported as deleted and geometry is not computed.",
        ),
        configuration=(
            "NotificationIndex requires FreeRTOS >=11.1 support in the struct.",
        ),
        limitations=(
            "The message-length prefix macro is not in DWARF; GDR assumes sizeof(size_t) and labels it.",
            _DISCOVERY_LIMIT,
        ),
        action="detail",
        kind="streambuffer",
    )
