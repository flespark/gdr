"""Structured FreeRTOS command documentation."""

from __future__ import annotations

from freertos.layout import FreeRtosLayout
from gdr.help import (
    CommandHelp,
    HelpField,
    HelpTopic,
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
_PROBLEM_FIELD = (
    "Check[name]",
    "Specific failure or incomplete-check evidence only; frt checks shows all verdicts.",
)


_HEAP_DUMP = HelpTopic(
    name="dump",
    summary="Draw the heap block bitmap",
    description=(
        "Draws the linear block walk as coloured cells: grey for free blocks, one "
        "neutral colour for allocated blocks (FreeRTOS keeps no owner names). Inline "
        "in iTerm2 or Kitty; otherwise written to a PNG file."
    ),
    usage=("frt heap dump [path]",),
    fields=_fields(
        (
            "Cells",
            "The arena split evenly into a fixed grid, row by row; a block paints every cell it touches.",
        ),
        (
            "File",
            "Default gdr-heap-freertos-gen<N>.png in the working directory, or the given path.",
        ),
    ),
    tips=(
        "GDR_HEAP_IMAGE=iterm|kitty|file overrides terminal detection.",
        "heap_3 wraps the C library malloc; the bitmap is unavailable there.",
    ),
    limitations=(
        "Only the linear walk is drawn; a free-list-only walk has no allocated blocks to show.",
    ),
)


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
                (
                    "Stack",
                    "Recorded usable stack span through pxEndOfStack, inclusive, in bytes; excludes alignment-discarded allocation padding.",
                ),
                ("Used", "Currently used stack bytes."),
                (
                    "HighWater",
                    "Historical maximum stack bytes used: max(Used, recorded usable span minus untouched 0xa5 fill bytes); includes initial context reservations before scheduler startup.",
                ),
                ("Runtime", "Raw run-time counter when statistics are enabled."),
                ("CPU", "Current SMP core, when configured."),
                ("Affinity", "SMP core-affinity mask, when present."),
                (
                    "PC",
                    "Last PC as <symbol+offset>, proven from the saved context frame (Cortex-M and RISC-V ports). N/A when no proof admits it.",
                ),
                ("Addr", "Task control block address; the row identity."),
            ),
            tips=(
                "Use `frt task <name-or-address>` for blocking, notification and consistency details.",
                'Use `p $gdr_task("<name-or-address>")` for native TCB field inspection.',
            ),
            configuration=(
                "BasePrio, stack bounds, Runtime, CPU and Affinity appear only when their TCB fields/configuration exist.",
                "HighWater requires pxEndOfStack (configRECORD_STACK_HIGH_ADDRESS) and stack prefill; appears only when at least one task has both.",
            ),
            limitations=(
                "There is no Entry column: a FreeRTOS TCB does not retain the task entry function after creation.",
                "A proven initial frame can show an entry PC before startup; this does not mean the task ran, nor provide a retained TCB entry field.",
                "Current saved-SP reservations count even when slots still contain fill, so HighWater >= Used. Historical reservations that left no write trace cannot be recovered from a halted snapshot.",
            ),
            aliases=("threads",),
            action="tasks",
            kind="task",
        ),
        CommandHelp(
            "task",
            "Show one task in detail",
            "Displays scheduler, stack, blocking, notification, run-time and consistency data for one task as Key: Value pairs.",
            usage=("frt task <name-or-address>",),
            fields=_fields(
                ("Name/Address/Type", "Identity and Idle/Normal classification."),
                (
                    "State/Priority/BasePriority",
                    "Scheduler state and effective/base priorities.",
                ),
                (
                    "SP/Stack/StackSize/Used/HighWater",
                    "Stack geometry and watermark data, sizes in bytes.",
                ),
                ("WakeTick/BlockedOn", "Blocking deadline and host when available."),
                ("Notify[n]", "Notification value and state for each configured slot."),
                ("Runtime/Runtime%", "Raw and percentage run time when enabled."),
                _PROBLEM_FIELD,
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
            "check",
            "Run explicit consistency checks",
            "Reports each pass, failure and skip with the object address and concrete evidence. Normal views show only problems.",
            usage=(
                "frt check [system|heap|task|queue|semaphore|mutex|timer|eventgroup] [name]",
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
            limitations=(_CHECK_LIMIT,),
            aliases=("checks",),
            action="check",
        ),
        CommandHelp(
            "watch",
            "Print watched objects on every stop",
            "Prints a watched kind's list table, or one object's Key: Value detail, each time the target stops. Like GDB display, not a watchpoint: it never stops the target itself.",
            usage=(
                "frt watch",
                "frt watch <kind>",
                "frt watch <object> <name-or-address>",
            ),
            fields=_fields(
                ("Kind", "A plural list command (tasks, queues, timers, ...)."),
                ("Target", "A singular detail selector: name, address or symbol."),
                ("Output", "One [gdr watch: ...] block per watch, in the order added."),
            ),
            tips=(
                "Names may contain spaces, for example `frt watch task Tmr Svc`.",
                "A singleton must exist when added; a later detach renders as gone instead of a stale address.",
            ),
            configuration=(
                "Watchable kinds follow the discovery channels available in this build.",
            ),
            limitations=(
                "Long tables and `set pagination on` can hold the prompt at every stop; keep watches narrow.",
                "Watches are session state and are dropped by `gdr reset`.",
                _DISCOVERY_LIMIT,
                _CHECK_LIMIT,
            ),
            action="watch",
        ),
        CommandHelp(
            "unwatch",
            "Drop stop-time watches",
            "Removes one watch, or every watch with --all.",
            usage=(
                "frt unwatch <kind> | <object> <name>",
                "frt unwatch --all",
            ),
            tips=("Run bare `frt watch` to list the set with its labels.",),
            limitations=(_CHECK_LIMIT,),
            action="unwatch",
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
                (
                    "Check[name]",
                    "Specific failure or incomplete-read evidence; use frt check for all results.",
                ),
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
            usage=("frt heap", "frt heap dump [path]"),
            children=(_HEAP_DUMP,),
            fields=_fields(
                ("Algorithm", "heap_1..heap_5, heap_3, or none."),
                (
                    "TotalSize/FreeSize/MinEver",
                    "Kernel size and free-space counters, in bytes.",
                ),
                (
                    "Allocated/Freed",
                    "Cumulative successful allocation/free operation counts, not byte sizes or live block counts; N/A when not exported.",
                ),
                ("Protector", "Heap-protector canary state when enabled."),
                ("Blocks/Holes", "Bounded walk counts and fragmentation."),
                (
                    "Check[HeapCrossCheck]",
                    "Only failure/incomplete evidence, with affected blocks or counters; absent when healthy. frt check heap includes passes.",
                ),
                (
                    "Address/Size/State|Next",
                    "Optional physical-block or free-list rows.",
                ),
            ),
            tips=(
                "Use frt check heap for complete consistency results; inspect reported mismatch/corrupt evidence before continuing the target.",
                "Compare MinEver with current FreeSize to distinguish a transient peak from current pressure.",
            ),
            configuration=(
                "The available counters and walk strategy depend on heap_1..heap_5 and configENABLE_HEAP_PROTECTOR.",
            ),
            limitations=(
                "FreeRTOS block headers have no owner field, so heap use cannot be attributed per task.",
                "heap_3 wraps libc and exposes no FreeRTOS arena to walk.",
                "heap_5 without the protector exports no reliable linear region bounds; Check[HeapCrossCheck] explains the skipped comparison.",
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
            "Use `frt queue <name-or-address>` for FIFO storage addresses; `frt checks queue <name-or-address>` for full consistency results.",
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
        "Show queue details",
        "Shows one queue as Key: Value pairs, including geometry, waiters, problems and FIFO storage addresses.",
        usage=("frt queue <name-or-address>",),
        fields=_fields(
            ("Head/Tail/WriteTo/ReadFrom", "Queue storage ring pointers."),
            ("Items/Length/ItemSize/Free", "Occupancy geometry."),
            ("SendWait/RecvWait/Locks/Set", "Blocking and deferred-operation state."),
            _PROBLEM_FIELD,
            (
                "Item[n]",
                "FIFO storage address for each queued item; inspect bytes through GDB or the memory viewer.",
            ),
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
        "Show semaphore details",
        "Shows one semaphore as Key: Value pairs, including counts, take waiters, locks and problems.",
        usage=("frt semaphore <name-or-address>",),
        fields=_fields(
            ("Count/Max", "Available tokens and capacity."),
            ("Waiters", "Tasks blocked on take."),
            ("Locks", "Deferred queue unlock counters."),
            _PROBLEM_FIELD,
            _SOURCE_FIELD,
        ),
        tips=("Use waiter names to continue with `frt task <name-or-address>`.",),
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
            "Compare Owner and waiter priorities in `frt mutex <name-or-address>` when diagnosing inversion.",
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
        "Show mutex details",
        "Shows one mutex as Key: Value pairs, including holder priorities, recursion, waiters, locks and problems.",
        usage=("frt mutex <name-or-address>",),
        fields=_fields(
            ("Held/Owner", "Ownership state and task name."),
            (
                "OwnerPriority/OwnerBasePriority",
                "Effective and base holder priorities.",
            ),
            ("RecursiveCallCount", "Recursive nesting depth."),
            ("Waiters/Locks", "Blocked takers and deferred queue state."),
            _PROBLEM_FIELD,
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
            "Use `frt timer <name-or-address>` to inspect active-list membership and queued daemon commands.",
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
        "Show software timer details",
        "Shows one timer as Key: Value pairs, including timing, list membership, ownership and pending commands.",
        usage=("frt timer <name-or-address>",),
        fields=_fields(
            ("State/Mode/Period", "Current evidence, reload mode and period."),
            ("Expiry/ExpiresIn", "Deadline and wrap-safe delta; N/A when dormant."),
            ("Callback/ID/List", "Callback, application ID and current/overflow list."),
            ("OwnerCheck", "List-item ownership verdict."),
            _PROBLEM_FIELD,
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
        tips=("Use `frt eventgroup <name-or-address>` to decode each wait condition.",),
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
        "Show event group details",
        "Shows one event group as Key: Value pairs, decoding bits and each waiter's ANY/ALL, clear-on-exit and missing-bit condition.",
        usage=("frt eventgroup <name-or-address>",),
        fields=_fields(
            ("Bits/Waiters", "Current mask and waiter count."),
            ("StaticallyAllocated", "Allocation mode when the field exists."),
            (
                "Waiter[n]",
                "Per-waiter task, wanted bits, mode, clear-on-exit and missing bits.",
            ),
            _PROBLEM_FIELD,
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
                "Derived ring occupancy and trigger level, in bytes.",
            ),
            ("NextMsg", "Next message length for message buffers."),
            ("RecvWait/SendWait", "Single waiting task handles."),
            _SOURCE_FIELD,
            _ADDR_FIELD,
        ),
        tips=(
            "Use `frt streambuffer <name-or-address>` for trigger and bounds diagnostics.",
        ),
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
        "Show stream/message buffer details",
        "Shows one buffer as Key: Value pairs, including ring geometry, triggers, waiters, message-prefix assumptions and bounds checks.",
        usage=("frt streambuffer <name-or-address>",),
        fields=_fields(
            (
                "Type/Capacity/Bytes/Space",
                "Classification and ring geometry, in bytes.",
            ),
            (
                "Trigger/TriggerMet",
                "Trigger level in bytes and current comparison result.",
            ),
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
