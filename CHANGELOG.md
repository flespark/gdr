# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- FreeRTOS `frt timers` prints its own column-contract table
  `Name State Mode Period Expiry ExpiresIn Callback ID Src Addr` instead of the
  neutral Kind/Count fallback. Timers reachable from the daemon's active lists
  come first, dormant ones (found through a static timer buffer or a global
  handle) after them; a dormant row's `Expiry`/`ExpiresIn` is `N/A` because its
  list-item value is a stale leftover, and the messages above the table carry
  the kernel tick, the provenance breakdown and the enumeration limit.
- FreeRTOS `frt timer <name>` vertical detail: `List` names the epoch the timer
  sits in (`current(xActiveTimerList1)` / `overflow(xActiveTimerList2)` /
  `none`, because the two list pointers swap when the tick count wraps),
  `OwnerCheck` reports `ok` / `mismatch` / `uninitialised` (decided by the list
  item's container member, since a never-started timer's `pvOwner` is
  uninitialised heap content), and a `Commands` section shows the pending
  daemon commands.
- FreeRTOS timer daemon command queue inspection (`freertos/timers.py`): the
  `xTimerQueue` ring is read FIFO with item-size and DWARF-type prechecks, each
  slot is cast to `DaemonTaskMessage_t`, and the 12 `tmrCOMMAND_*` ids are
  rendered by name in a `Seq Command Timer Value` table. An empty queue says
  `no pending timer commands`; a queue that reports waiting messages but cannot
  be walked says `timer command queue slots unreadable` rather than looking
  empty. The `xMessageID < 0` pended-callback arm is decoded only when the
  `u.xCallbackParameters` member exists in DWARF (it is gated by
  `INCLUDE_xTimerPendFunctionCall`); otherwise the row states
  `pended-callback arm absent`.
- FreeRTOS timer consistency checks (`timer_checks`) shown as a `Checks` line in
  the timer detail: `ucStatus`-vs-list sync, nonzero period and callback, and
  the daemon queue item size against `sizeof(DaemonTaskMessage_t)`; each check
  reports `ok`, `fail: <what>` or `skipped: <why>`.
- FreeRTOS `Timer_t` layout gained `pvTimerID`, `pxCallbackFunction`,
  `ucStatus` and `xTimerListItem`; `uxTimerNumber` is registered only on
  `configUSE_TRACE_FACILITY` builds (it is absent from DWARF otherwise) and is
  never used as an identity.
- FreeRTOS `frt queues` / `frt semaphores` / `frt mutexes` print their own
  column-contract tables instead of the neutral Kind/Count fallback:
  `Name Type Items Length ItemSize Free SendWait RecvWait Locks [Set] Src
  Addr` for queues, `Name Type Count Max Waiters Src Addr` for semaphores and
  `Name Type Held Owner Recursive Waiters Src Addr` for mutexes. The `Set`
  column exists only on `configUSE_QUEUE_SETS` builds, where it holds the
  queue-set address of a member.
- FreeRTOS `frt queue|semaphore|mutex <name>` vertical details: the queue view
  dumps the pending items in FIFO order (`Item[i]`, starting at
  `pcReadFrom + uxItemSize`, wrapping at `pcTail`, payload truncated to 64
  bytes), the mutex view adds `Held`/`Owner`/`OwnerPriority`/
  `OwnerBasePriority`/`RecursiveCallCount`, and the semaphore view
  deliberately has no owner field at all (its `u.xSemaphore` arm is not a
  holder record).
- FreeRTOS queue-family consistency checks (`freertos/diagnostics.py`) shown as
  a `Checks` line in every queue-family detail: `Count`, `Storage`,
  `WritePtr`, `ReadPtr`, `MutexAccounting`, `SemaphoreSelfHead`. Each check
  reports `ok`, `fail: <what>` or `skipped: <why>`; the storage-window pointer
  invariants only apply to real data queues, so they are explicitly skipped
  for mutexes and semaphores rather than fabricating failures.
- FreeRTOS `Queue_t` discrimination: the exact kind comes from `ucQueueType`
  when `configUSE_TRACE_FACILITY` is on; otherwise the `pcHead == NULL` mutex
  marker and `uxItemSize == 0` semaphore marker still separate the family and
  the `Type` cell carries a trailing `?` to mark the inference.
- FreeRTOS object discovery: six provenance channels (optional `xQueueRegistry`,
  static/handle global symbols found by one cached `info variables` scan, the
  active timer lists, the MPU wrappers v2 `xKernelObjectPool`, reverse
  `container_of` from blocked tasks' `xEventListItem`, and explicit user
  addresses/symbols), merged by address with a fixed channel priority.
- FreeRTOS `frt objects` prints a `Kind`/`Count`/`Sources` summary with a
  `source=count` breakdown and the enumeration limitation above the table; on
  `configQUEUE_REGISTRY_SIZE 0` builds it also states why the registry channel
  is unavailable.
- `$gdr_object(kind, name)` works for FreeRTOS kinds beyond `task`, accepting a
  discovered name, a `0x` or decimal address, or a global symbol name, and
  returning the target-native `gdb.Value`.
- FreeRTOS Tab completion for singular detail commands now offers discovered
  object names for every kind, not only tasks.
- FreeRTOS pretty-printers: `p *pxCurrentTCB` now folds to
  `Task(current_priority=0, name="IDLE", base_priority=0)`, with similar
  folds for `List(...)`, `Queue(...)`, `Timer(...)`, `EventGroup(...)`,
  `StreamBuffer(...)`, and `ListItem(...)`.
- FreeRTOS config probing: 14 new capability fields (`heap_kind`,
  `max_priorities`, `queue_registry_size`, `mpu_object_pool`,
  `list_integrity_check`, `event_groups`, `stream_buffers`,
  `stream_buffer_notification_index`, `queue_sets`, `task_attributes`,
  `preemption_disable`, `critical_nesting_in_tcb`, `posix_errno`,
  `delay_abort`, `heap_protector`).
- RTOS-neutral `read_macro_text(name)` bridge primitive for reading GDB
  macro text without CU-specific assumptions.
- FreeRTOS command tree expanded: 7 plural list commands (`tasks`, `queues`,
  `semaphores`, `mutexes`, `timers`, `eventgroups`, `streambuffers`), 7
  singular detail commands (`frt task <name>`, etc.), standalone `help`,
  `system`, `objects`, `heap`, and 6 aliases (`threads`/`sems`/`mtxs`/`qs`/
  `egs`/`sbs`).
- FreeRTOS `frt task <name>` vertical detail with per-TCB state, high-water
  mark, notification slots, wake tick, blocked-on, and mutexes-held.
- FreeRTOS per-TCB state determination aligned with `eTaskGetState`
  (Running/Ready/Blocked/Suspended/Deleted, SMP Running(yielding)).
- FreeRTOS `HighWater` column in `frt tasks`: stack-fill scan using
  `[pxStack, pxTopOfStack)` window (no `pxEndOfStack` required).
- FreeRTOS Tab completion via Python `complete()`: command vocabulary as
  first word, live task names for singular detail commands.
- FreeRTOS object names with spaces now reachable (e.g. `frt task Tmr Svc`).

### Changed

- FreeRTOS queue-family objects found through the registry, MPU pool or waiter
  channels are now refined to their real kind and deduplicated across
  queue/semaphore/mutex, so `frt objects` no longer counts a mutex or
  semaphore as a queue and one address never appears in two tables.
- FreeRTOS object discovery scans the waiter channel once per command instead
  of once per kind (`frt objects` previously walked every task list six
  times). Nothing is cached across commands, since target memory keeps moving.
- FreeRTOS name resolution prefers names: a purely numeric argument is looked
  up as a discovered name or global symbol first, and decimal address parsing
  is only the fallback. `0x`-prefixed addresses are unchanged.
- `read_cstring` reads `char*` values as a bounded window truncated at the
  first NUL. The previous implementation dereferenced the pointer first and
  always returned `None` for pointer-typed names, which hid every `char*`
  kernel object name (such as `pcQueueName`); RT-Thread names are `char[]`
  arrays and are unaffected (re-verified across the Cortex-A9 matrix).
- FreeRTOS single-kind list commands (`frt queues`, `frt semaphores`, …) no
  longer warn that the kind is not reliably enumerable; the queue family and
  timers have full tables and details, while `eventgroups`/`streambuffers`
  still print the neutral `Kind`/`Count` table.
- FreeRTOS symbol-channel timers are named by their `pcTimerName` instead of the
  handle or static-buffer variable name, so a dormant timer is listed and
  resolvable (`frt timer <name>`) under the name the firmware gave it. This
  changes the row names in `frt timers` and the timer counts' source breakdown
  in `frt objects`.
- FreeRTOS supported version range is now continuous `(10,3,0)-(11,2,99)`;
  previously rejected versions such as 10.4.x, 10.7+, and 11.2.x are now
  accepted.
- Pretty-printer type matching now resolves typedef and cv-qualified values
  to their underlying struct tag (`strip_typedefs().unqualified().tag`),
  fixing fold for all FreeRTOS kernel objects which are typedef-spelled.

### Fixed

- StreamBuffer summary field path corrected from `uxLength` (Queue member)
  to `xLength` (actual `StreamBufferDef_t` member).
- Queue `type` summary field is now gated by `cfg.trace_facility`; builds
  with `configUSE_TRACE_FACILITY=0` no longer show `type=N/A`.
- `_array_bound` treats zero or negative DWARF range as unknown (`None`)
  instead of writing `0` into `number_of_cores` / `max_priorities`.
- FreeRTOS boundary regex in `test_boundary.py` tightened to catch long
  identifiers (`tsk[A-Za-z0-9_]+`).

### Removed

- `FreeRtosTask.entry` field and `struct xTASK_STATUS` layout (zero
  consumers; FreeRTOS TCB does not store entry function).
- `mpu_wrapper_v2` config field (replaced by `mpu_object_pool` with correct
  semantics).

## [2026.02] - 2026-08-22

### Added

- Added RTOS-neutral `$gdr_task`, `$gdr_tasks`, and `$gdr_object` functions,
  plus adapter-owned task/object tables and a unified command router.
- Added RT-Thread detail commands for tasks, timers, IPC, and memory pools,
  including bounded waiter, capacity, policy, expiry, and consistency data.
- Added width-aware table rendering, target-symbolized function pointers, and
  a profile-driven QEMU/GDB harness with persistent sessions and diagnostics.
- Added RT-Thread heap command for heap info collection and diagnostic (heap
  algorithm, used size, total size, owner, corrupt check, memory bubble state).

### Changed

- Release archives now contain both RT-Thread and FreeRTOS adapters in one
  `gdr` asset. The `gdr` command is limited to initialization and help; RTOS
  commands remain under `rtt` and `frt`.
- Task and IPC tables expose capability-aware columns such as `BasePrio`,
  `Addr`, `CPU`, `Bind`, waiter lists, `Free`, `Used`, and `ExpiresIn`.
- Table widths shrink only marked text columns and preserve the complete
  column set across terminal sizes.

### Fixed

- Completed RT-Thread routing and object tables for events, mailboxes, message
  queues, and memory pools.
- Fixed SMP CPU-0 reporting and retained detail diagnostics when raw fields or
  waiters are unavailable; unsupported values now render explicitly as `N/A`.

## [2026.01] - 2026-07-29

### Added

- RT-Thread 3.1.x and 4.x support, with layout and kernel configuration
  probing for SMP, heap managers, and IPC components.
- GDB aggregate commands, pretty-printers, and convenience functions for
  RT-Thread objects.
- Closed-loop QEMU verification for Cortex-A9 and RISC-V RV64 targets.

### Changed

- Function-pointer columns in `rtthread threads` and `rtthread timers` now
  resolve target symbols while preserving a hexadecimal address fallback.
- GDB command registration and table output now provide clearer diagnostics
  for malformed target data and unavailable symbols.
