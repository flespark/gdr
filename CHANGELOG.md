# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- FreeRTOS `frt heap` now renders the real heap snapshot instead of the
  placeholder: `Algorithm` (heap_1..heap_5 plus the heap_3/none split on
  `pvPortMalloc` presence), `TotalSize`/`FreeSize` from the kernel's own
  counters, the heap_4/5 `MinEver`/`Allocs`/`Frees`, `Protector` (the
  `xHeapCanary` XOR decode of every `pxNextFreeBlock` including the chain
  head), the free-list `Blocks`, the linear-walk `Holes`, and `CrossCheck`, a
  three-way consistency verdict between the free-list byte sum, the linear
  free-byte sum and `xFreeBytesRemaining` (plus free-block address sets) that
  reports the concrete figures on mismatch and never synthesises a plausible
  total. The block-header size comes from `xHeapStructSize`/`heapSTRUCT_SIZE`
  with an `align_up(sizeof(BlockLink_t), portBYTE_ALIGNMENT)` fallback; the
  allocation mask is computed from `sizeof(size_t)` and is off for heap_2
  before V10.5.0 (no MSB bit existed). heap_2's free list terminates at the
  `xEnd` *value*, heap_4/5 at `pxEnd`; heap_5 counts its zero-size region
  link blocks but skips them for the smallest statistic. `frt system` gains
  `Heap used`/`Heap total`/`Heap status` (`good`/`corrupt`) and splits the
  allocator label into `heap_3` vs `unavailable`. Help and README document
  that FreeRTOS block headers carry no owner field, so no per-task heap
  attribution is offered; heap_5 without the heap protector has no region
  bases, so its linear walk is skipped with an explicit `CrossCheck` reason.
  The linear walk starts at the kernel's own `align_up(&ucHeap)` base rather
  than at the free-list head - heap_4 carves allocations from the front of the
  first free block and heap_2 sorts its list by size, so the head sits above
  the blocks at the heap base - and is skipped outright when `ucHeap` cannot be
  resolved. `frt system` omits `Heap used` while the heap is still
  uninitialised (the counters hold static initializers, so `total - free` would
  claim the whole heap is in use) and keeps only the knowable `Heap total`.

- FreeRTOS `frt eventgroups` prints its own column-contract table
  `Name Bits Waiters Src Addr` instead of the neutral Kind/Count fallback, and
  `frt eventgroup <name|addr>` renders one line per blocked task:
  `wants=0x.. mode=ALL|ANY clearOnExit=yes|no missing=0x..`. The control bits
  that share `xItemValue` with the wanted bits are compile-time macros with no
  debug info, so every mask is derived from the tick width
  (`EventBits_t == TickType_t`) rather than assuming 32 bits. A waiter whose
  bits are already set but is still on the wait list is marked
  `(satisfied — mid-unblock)` — the kernel unblocks such a task by removing
  the item, so this state means the setter has not finished running.
  `StaticallyAllocated` appears only on builds where the kernel declares
  `ucStaticallyAllocated` (static *and* dynamic allocation both enabled).
- FreeRTOS `frt streambuffers` prints its own column-contract table
  `Name Type Bytes Space Capacity Trigger NextMsg RecvWait SendWait Src Addr`,
  and `frt streambuffer <name|addr>` adds `TriggerMet` (batching buffers use
  `>`, every other kind `>=`), `MsgLenBytes`, `NotificationIndex` and
  `BoundsCheck`. Bytes and space use the kernel's own wrap-safe arithmetic;
  `Capacity` is `xLength - 1` (only the dynamic create path adds the extra
  byte, so a static buffer's capacity is one less than an equally sized
  dynamic one). `NextMsg` is read as the length prefix at `pucBuffer + xTail`
  with the kernel's two-part ring wrap and is `N/A` for non-message buffers;
  `MsgLenBytes` is labelled as an assumed `size_t` because
  `sbBYTES_TO_STORE_MESSAGE_LENGTH` leaves no debug info. A buffer with
  `xLength == 0` and a readable NULL `pucBuffer` is reported as `deleted`
  instead of computing geometry from a zero length. Stream buffers keep their
  waiters in single task-handle fields rather than lists, so they have no
  waiter discovery channel — an unregistered dynamic buffer with no global
  handle can never be enumerated, and the commands say so.
- FreeRTOS `EventGroupDef_t` and `StreamBufferDef_t` layouts gained their
  detail fields; `uxEventGroupNumber` / `uxStreamBufferNumber` are registered
  only on `configUSE_TRACE_FACILITY` builds, `ucStaticallyAllocated` only when
  both allocation modes are on, and `uxNotificationIndex` only on kernels
  >= 11.1.0 (verified by comparing `ptype` output on a 10.3.1 and an 11.1.0
  fixture: `sizeof(StreamBufferDef_t)` 36 vs 40).
- FreeRTOS fixture: a task now blocks forever on an event group whose handle
  lives only on its own stack, giving the `waiter` discovery channel its first
  object that no other channel can find. The live no-ghost assertion allows
  exactly that one anonymous event group and still fails if the channel
  fabricates hosts out of neighbouring memory.

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
- FreeRTOS fixture: a new dual-core SMP lane builds kernel 11.3.1 for the
  QEMU mps2-an521 board (ARMv8-M, `portable/GCC/ARM_CM33_NTZ/non_secure`,
  the first kernel tag whose `portVALIDATED_FOR_SMP` is 1), with one task
  pinned to core 1 and one running with preemption disabled, so the
  `Affinity`/`PreemptionDisable` cells and the core-affinity probes carry
  live fixture state.  Cross-core yields are not exercised: the port's weak
  `vInterruptCore` is a no-op on this board and every ground-truth waiter
  is pinned to core 0 for deterministic scheduling.
- FreeRTOS snapshot fixture enables `configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES`,
  so `ListIntegrityBytes` verifies against a real `List_t` instead
  of always reporting `skipped`: every list (and the mini `xListEnd` item)
  is stamped with `pdINTEGRITY_CHECK_VALUE` and a dedicated negative list
  corrupts only `xListIntegrityValue1` (0xdeadbeef vs 0x5a5a5a5a) -- the
  first live fixture for the failure arm.
- FreeRTOS fixture variant `pend-callback` enables
  `INCLUDE_xTimerPendFunctionCall` and suspends the timer daemon before
  enqueueing a pended callback plus six timer commands, so the daemon
  queue's negative-ID slot and a ring-wrapping non-empty command queue
  survive to the harness breakpoint; the timer detail's `Commands` table
  decodes the real `xCallbackParameters` (function + both arguments) when
  the union arm exists in DWARF.
- FreeRTOS fixture variant `streams` writes/reads/deletes its
  stream/message/batching buffers before the ready marker: exactly the
  trigger level into the stream and batching buffers (the `>=` vs `>`
  asymmetry, on V11.1+ cells where `xStreamBatchingBufferCreate` exists),
  a wrapping message-buffer ring (`xHead < xTail`) whose
  `NextMsg` is the first message length, and a static buffer deleted while
  its handle symbol survives (`Type: deleted`).
- FreeRTOS fixture variant `heap-5-protector` links heap_5 with
  `configENABLE_HEAP_PROTECTOR` and one heap region, so the protector's
  region extremes become the true heap total (`TotalSize` no longer
  `unavailable`) and the linear walk + `CrossCheck: ok` get live heap_5
  evidence.
- FreeRTOS version lane `mps2-an385/11.1.0/heap-2` pins a live heap_2
  grid on the V10.5+ semantics (the allocation MSB in `xBlockSize`), where
  the previous live heap_2 cell (10.3.1) predates the bit.
- FreeRTOS fixture: a new 64-bit lane on QEMU `-machine virt`
  (`qemu-system-riscv64` + `portable/GCC/RISC-V`, rv64imac, SiFive CLINT
  tick at 10 MHz) gives the discovery layer, queue-family decode and the
  heap `size_t` MSB mask their first live 64-bit evidence; the RISC-V port
  always uses 64-bit ticks, so the event-group decode runs on a 64-bit
  tick width too.
- FreeRTOS fixture variant `mpu` (single-core CM33 on mps2-an521 with
  `configENABLE_MPU` and `configUSE_MPU_WRAPPERS_V1 0`) builds and links the
  MPU wrappers v2, but its boot is not stable under QEMU's SSE-200 model
  (UsageFault in the wrappers-v2 SVC path with clean fault-status registers),
  so it is documented as unreachable under QEMU and stays out of CI: the
  `mpu-pool` discovery channel keeps unit-test coverage (on real hardware
  the pool would be populated by every plain task or queue create, since the
  headers macro-rewrite the creates to their `MPU_` counterparts).
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

- The waiter channel's event-group plausibility check no longer scales its
  threshold with the tick width: on a 64-bit tick the old `(1 <<
  (tick_bits - 8))` bound was `(1 << 56)` and every RAM pointer passed it,
  so each queue with a blocked receiver forged a ghost event group (RV64
  probe: `Bits` column showing the neighbour pointer).  The pointer
  rejection now delegates to the loadable-section check `_mapped_ranges()`
  (width-independent; values below the 24 user event bits are exempt so a
  quiescent group on a board that maps flash at 0 stays accepted), keeping
  the control-byte check for genuine bit values.
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
