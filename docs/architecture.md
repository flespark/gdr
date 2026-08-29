# Architecture

## Goals

Reduce the cognitive load of debugging complex RTOS-based embedded
firmware in GDB by:

- Folding noisy wrapper-type output (pretty-printers).
- Solving object navigation once (convenience functions).
- Aggregating multi-object state and Debug orientated diagnostics data gather (commands).

Non-goals: replacing GDB expressions, wrapping QEMU monitor commands, or
duplicating what `rust-gdb` / `gdb` already display well.

## Layering

```text
                     gdr.py  (entry: arg parse, bootstrap, register)
                        |
        +---------------+----------------+
        |                                |
       gdr/  (RTOS-agnostic core)      selected RTOS adapter
         |                                |
  adapter_api.py / commands.py      layout.py / navigation.py
  formatting.py / gdb_bridge.py     adapter.py / diagnostics.py
  layout.py / printers.py           commands.py / version.py
```

### `gdr/` — core

| Module | Responsibility |
| -------- | --------------- |
| `gdb_bridge.py` | Wraps GDB registration, identifier-only symbol lookup, memory/type access, terminal probing, output and error guards. Inferior function evaluation is not used. `read_cstring` reads `char[]` to the terminator GDB finds, and reads `char*` as a bounded window (`GDR_MAX_CSTRING_LENGTH` bytes, `errors="replace"`) truncated at the first NUL, because a pointer carries no length; a short name whose window crosses into unmapped memory therefore degrades to `None`. |
| `constants.py` / `formatting.py` | Shared traversal/presentation defaults and pure optional/address/symbol/table formatting. Formatting has no GDB dependency. |
| `layout.py` | Generic `StructLayout` / `StructField` / `ListHook` dataclasses and accessors (`read_field`, `iter_list`, `container_of`). It interprets adapter-supplied paths but contains no target type names, symbols, or list conventions. |
| `printers.py` | Generic pretty-printer registration and rendering. Display labels, summary fields, enum maps, and pointee display paths come from the adapter layout. Type matching uses `strip_typedefs().unqualified().tag` so that typedef-spelled and cv-qualified values resolve to their underlying struct tag. |
| `version.py` | RTOS-neutral version parsing, range checks, formatting and declared decimal/packed-hex decoding. |
| `adapter_api.py` | `RtosAdapter`, `ObjectTable`, `ObjectDetail`, `SystemSummary`, and the single active adapter selected by `gdr init`. |
| `commands.py` / `functions.py` | Generic output coordination and raw-value convenience functions; task columns and object vocabulary remain adapter-owned. |

### `rtthread/` — adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | **The only place that knows RT-Thread struct layouts.** Defines `RtConfig`, `detect_config()` (symbol-presence probing), and `build_layouts(config) -> KernelLayout`. Handles config-conditional fields (SMP, heap manager, IPC toggles) via factory branches, not version-branched files. |
| `navigation.py` | RT-Thread object navigation: registry/current-thread/tick entry symbols, type codes, timer traversal, and the halted system-heap snapshot. Returns raw `gdb.Value` objects using the layouts supplied by `layout.py`. |
| `adapter.py` | RT-Thread intermediate object models, `gdb.Value` conversion, adapter-owned task/object tables, detail dispatch and system summaries. These models are presentation inputs, not ABI layout descriptions. |
| `diagnostics.py` | Consumes adapter intermediate models for detail rendering and performs bounded raw mailbox/message-queue/mempool and system-heap walks and consistency diagnostics. |
| `version.py` | RT-Thread support ranges, exported target symbols, encoding order and RT-Thread-specific diagnostics. |
| `commands.py` | The `rtthread` / `rtt` command tree, including routing, aliases, `rtt help`, and `rtt heap`. |

### `freertos/` — adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | FreeRTOS config/DWARF probes, logical struct paths, `FreeRtosLayout`, and the complete `FreeRtosTask`-related capability metadata. Config and layout stay together because the detected TCB fields directly determine the built paths. Version detection relies on `-g3` macro debug info and is subject to CU scope limitations (see known constraints below). |
| `navigation.py` | Pure scheduler-list and current-task traversal functions plus the object discovery channels (`iter_registry_entries`, `iter_static_symbol_objects` with its session cache, `iter_active_timer_hosts`, `iter_mpu_pool_objects`, `iter_waiter_hosts`) and their aggregation (`DiscoveredObject`, `discover`, `discover_all`, `resolve_object`). Queue-family candidates are refined by the `Queue_t` discriminator (`classify_queue`: `ucQueueType` when `configUSE_TRACE_FACILITY` is on, else the `pcHead == NULL` mutex marker and `uxItemSize == 0` semaphore marker), deduplicated across the family so one address never lands in two tables; `discover_all` shares one waiter-channel scan per command call. The symbol channel names timer objects by their `pcTimerName` (read from the cast `Timer_t`) instead of the handle/buffer variable, so dormant timers stay reachable by the name the firmware gave them. List member access uses logical `end`/`next`/`owner`/`count` fields from `FreeRtosLayout`; walks are bounded and corruption-guarded. |
| `timers.py` | FreeRTOS software-timer decoding for the daemon's two active lists and its command queue (`xTimerQueue`): the current/overflow list epoch, the `pvOwner` vs object-address check (container decides `uninitialised`), the wrap-safe `ExpiresIn` formula for both epochs, and `iter_timer_commands()` which ring-reads the queue (item-size and DWARF-type prechecks, slots cast to `DaemonTaskMessage_t`) and maps the 12 `tmrCOMMAND_*` ids to names. |
| `events.py` | Event-group inspection: control-bit masks derived from `cfg.tick_bits` (the `CLEAR_ON_EXIT` / `UNBLOCKED_DUE_TO_BIT_SET` / `WAIT_FOR_ALL` / `CONTROL_BYTES` / `eventIN_USE` bits are compile-time macros with no DWARF), the raw `xEventListItem` decode for every task blocked on `xTasksWaitingForBits`, the ALL vs ANY `missing` computation, and the `(satisfied — mid-unblock)` marker for a waiter the kernel already unblocked. Feeds the `frt eventgroups` table and the `frt eventgroup <name>` detail via the `uchStaticallyAllocated` config gate. |
| `streams.py` | Stream/message/batching buffer geometry: the wrap-safe bytes (`prvBytesInBuffer`) and space formulas, the batching `>` vs plain `>=` trigger comparison, the six `ucFlags` classifications (static stays out of the table Type, so the column contract does not drift between variants), the `xLength == 0 && pucBuffer == NULL` deleted-buffer short-circuit, the `NextMsg` length-prefix read gated on the `size_t`-fallback assumption, and the single-`TaskHandle_t` waiter rendering (no waiter discovery channel exists for stream buffers). |
| `heap.py` | System-heap snapshotting for `frt heap` and the `Heap *` fields of `frt system`. Consumes `cfg.heap_kind` (the discriminator in `layout.py`); splits the `None` case into `heap_3` vs `none` by `pvPortMalloc` presence. Compute-only version gates: the `xHeapStructSize`/`heapSTRUCT_SIZE` symbol (computed from `align_up(sizeof(BlockLink_t), portBYTE_ALIGNMENT)` as fallback), the size_t-MSB `heapBLOCK_ALLOCATED_BITMASK` (off for heap_2 < V10.5.0), and the `xHeapCanary` XOR deobfuscation of every `pxNextFreeBlock` including the chain head. Bounded raw walks replicate `vPortGetHeapStats` semantics (never inferior-call the function): the free-list walk (terminates at `pxEnd` for heap_4/5, at the `xEnd` *value* for heap_2; heap_5 zero-size region link blocks count but skip the smallest-size statistic) and the linear walk (stepping by `xBlockSize`; allocation from the MSB, or free-list membership before the bit exists; started at the kernel's own `align_up(&ucHeap)` base, not at the free-list head, and skipped when that base is unknowable). `cross_validate` compares free-list bytes, linear free bytes and `xFreeBytesRemaining` plus the free-block address sets; any mismatch reports the three concrete numbers and never synthesises a plausible total. |
| `adapter.py` | The complete `FreeRtosTask` intermediate model, TCB conversion, adapter-owned task columns, system summary, and the object protocol methods (`find_object`, `object_counts`, the provenance summary table, the queue/semaphore/mutex list tables and their details, the `FreeRtosTimerObject` model feeding the `frt timers` table, and the event group / stream buffer tables delegated to `events` / `streams`). |
| `version.py` | FreeRTOS support ranges, exported target symbols, encoding order and FreeRTOS-specific diagnostics. |
| `commands.py` | The `freertos` / `frt` command tree: 7 plural list commands (`tasks`/`queues`/`semaphores`/`mutexes`/`timers`/`eventgroups`/`streambuffers`), 7 singular detail commands (`frt task <name>`, etc.), standalone `help`/`system`/`objects`/`heap`, and 6 aliases (`threads`/`sems`/`mtxs`/`qs`/`egs`/`sbs`). `objects` is rendered locally (`render_object_summary`) because the neutral core renderer has no provenance column; `queues`/`semaphores`/`mutexes`/`timers`/`eventgroups`/`streambuffers` render their own column-contract tables (`object_table()`), and `heap` renders `heap_report()` (algorithm/total/free/min/alloc/free counters, protector state, free-list block count, linear-walk holes and the three-way cross-check verdict, plus the optional block table). |
| `details.py` | FreeRTOS vertical detail rendering: `frt task <name>` (per-TCB state, high-water mark, notification slots, wake tick, blocked-on), the queue family (`frt queue/semaphore/mutex <name>`, including the FIFO `Item[i]` dump and mutex owner priorities), `frt timer <name>` (List epoch, OwnerCheck, and the pending daemon-command section), `frt eventgroup <name>` (per-waiter wants/mode/clearOnExit/missing decode), and `frt streambuffer <name>` (geometry, trigger-met verdict, NextMsg, the `size_t`-assumed length prefix, the version-gated NotificationIndex and the three-state bounds check). |
| `diagnostics.py` | Bounded raw list walking and the consistency checks the detail commands consume. `walk_list_raw` starts at `xListEnd.pxNext`, resolves `List_t`/`ListItem_t` member offsets from DWARF (so the optional integrity fields cannot shift it), and reports a cycle, a NULL `pxNext` (a healthy chain always links back to the sentinel, so zero is a cut chain, not an end), a node outside every loadable section, an unreadable item, or the `GDR_MAX_TRAVERSAL_COUNT` bound — never a clean short list. `list_checks`/`task_checks`: `ListInit`, `ListCount`, `ListIndex` (SMP only — on single core `pxIndex` legitimately parks on the rotation cursor, so the check is `skipped`), `ListIntegrity`, `ListIntegrityBytes` (magic derived from `cfg.tick_bits`, not a hard-coded `0x5a5a5a5a`), `ItemOwner`, `ItemContainer`, `StackFillPresent`. `system_checks` (rendered by `frt system`): `TaskCount`, `SchedulerSuspended`, `NextUnblockTime`, `HeapCrossCheck`. Queue-family checks (`queue_checks`): count bound and the storage-window pointer invariants for real data queues only; mutex accounting and semaphore self-head checks for those kinds; `QueueLock` (`cRxLock`/`cTxLock` must be `queueUNLOCKED` outside a `vTaskSuspendAll`, and an unreadable suspend counter is reported as unreadable rather than asserted to be zero). Timer checks (`timer_checks`): `ucStatus`-vs-list sync, nonzero period/callback, and the daemon queue item size vs `sizeof(DaemonTaskMessage_t)`. Event-group check (`event_checks`): `EventWaiterSatisfied`. Inapplicable checks are reported as explicit `skipped` instead of pass/fail. |

## Key decisions

### No RTOS / version auto-detection

Previous versions attempted to detect the RTOS and parse its version string
from symbols, then match struct patterns. This was fragile (failed on
attach, failed across remote configs) and duplicated logic. Users now
specify the RTOS and exact version, for example `gdr init rtthread 4.0.5`.

### Config features are probed, not specified

RTOS kernels vary by *configuration* far more than by version. For RT-Thread:
`RT_USING_SMP` adds `oncpu` to `rt_thread`; the heap manager
(`small_mem` / `slab` / `memheap`) changes the heap data structures
entirely; IPC components (`RT_USING_MUTEX`, etc.) may be absent.
Probing these by symbol presence (`rt_cpu_index`, `rt_sem_init`,
`rt_mutex_take`, ...) is reliable and cheap, and spares users from
reciting their `.config`. Probing falls back to safe defaults with a
warning when a symbol is ambiguous. Symbol presence uses DWARF/ELF
lookup, never a GDB expression that could call into the target.

### No inferior function calls

GDR inspects a halted RTOS. `gdb.parse_and_eval("foo()")` resumes the
core until the dummy frame returns, so SysTick and pending peripheral
ISRs can mutate `rt_tick`, thread state, and IPC objects. Kernel
collection therefore uses only:

- `gdb.lookup_symbol` / `lookup_global_symbol` / `lookup_static_symbol`
  for identifiers (`rt_tick`, `_object_container` / `rt_object_container`,
  `_cpus`)
- `gdb.Value` field and index access for struct members and arrays
- identifier-only `eval_identifier` → `gdb.parse_and_eval("NAME")` so GDB
  expands macros such as packed version constants on the host. Call and
  index expressions are rejected by the wrapper.

Target helpers such as `rt_tick_get()`, `rt_object_get_information()`,
`rt_hw_cpu_id()`, and `rt_memory_info()` are not called. SMP current-CPU
identity comes from the halted register file or GDB's selected thread.

### Adapter-owned dataclass layouts, not YAML schemas

Considered an external YAML schema + loader. Rejected because:

- Structs vary by **config**, not version. YAML would need conditional
  fields / overlays, turning the "lightweight loader" into a mini
  interpreter — a new failure surface.
- Version-to-version struct deltas are small; per-version YAML files
  would be 99% duplicate.
- Python dataclasses handle config-conditional fields naturally via
  factory functions (`build_thread_layout(config)`) with no extra
  syntax or parser.
- The adapter owns concrete type names, field paths, display labels, state
  encodings, target symbols, and object-registry traversal. The core only
  consumes generic layout metadata, so another RTOS can use different
  wrappers and object types without changing `gdr/`.

### Coupling is explicit and localised

All RT-Thread coupling lives under `rtthread/`: `layout.py` owns ABI field
paths, display metadata and state encodings; `navigation.py` owns symbols and
raw registry traversal; `adapter.py` owns intermediate presentation models,
conversion and tables; `diagnostics.py` consumes those models and owns bounded
raw-memory detail walks. Navigation remains pure-function based and receives a
layout explicitly only when it reads it. `gdr.py` is the composition root;
modules inside `gdr/` never import or identify an RTOS.

The dependency rules are enforced as follows:

- `gdr.py` may import the RTOS packages because it is the composition root.
- `gdr/` may depend only on Python/GDB and other `gdr/` modules; it must never
  import `rtthread` or `freertos`.
- An RTOS adapter may depend on `gdr/` contracts/helpers and modules inside
  its own package, but never on another RTOS adapter.
- `navigation.py` does not own adapter instances or hidden layout state. Its
  public walks are functions; callers pass a layout only when field paths or
  type mappings are required.
- Presentation flows inward as `adapter -> ObjectTable/ObjectDetail -> core
  renderer`; the core does not inspect RTOS model fields.

### Commands only aggregate

Per the Asterinas experience: commands should provide the multi-object
presentation that GDB expressions cannot conveniently produce: tables, trees,
and derived summaries. Convenience functions may navigate a collection, but
leave element inspection to native GDB expressions. Single-object field
inspection is left to `$gdr_task(name)` + `p $gdr_task(name).field`. This
keeps the command set small and avoids commands silently breaking when a field
is renamed (the function returns the raw `gdb.Value`).

## Main design

### Unified routing through the active adapter

`gdr/commands.py` holds the shared renderers, but every renderer resolves the
target through `adapter_api.active()` and dispatches on the active adapter's
`RtosAdapter` protocol (`task_table`, `object_table`, `object_detail`,
`system_summary`, `find_task`, `find_object`, `iter_tasks`). There is exactly
one routing entry point — the session adapter selected by `gdr init` — so
`gdr/` never branches on an RTOS name or hardcodes a command vocabulary. A new
RTOS only needs to implement the protocol to obtain the shared command tree,
pretty-printers and convenience functions unchanged.

### Exception guard for kernel data collection

RTOS debugging routinely touches target memory through GDB, and any read can
raise `gdb.error` / `gdb.MemoryError` (target halted, unmapped memory, remote
link dropped). Without a guard these bubble up as GDB "Python Exception"
noise and abort the rest of the command.

- **Command guards.** Every command tree's dispatch
  (`gdr._invoke_command` for ``gdr init``, and each adapter's
  `_invoke_command` for ``rtt``/``frt``) is wrapped with
  `@gdb_command_guard`. This is the one choke point that catches anything an
  inner renderer, adapter or navigation walk failed to contain — including
  `gdr init`'s config probing. The shared renderers in `gdr/commands.py` are
  also guarded, providing defense-in-depth and keeping direct calls safe.
- **Convenience-function guards.** `gdb.Function.invoke` must return a
  `gdb.Value`, so the command guard's `None` fallback cannot be used.
  Instead `gdb_function_guard` converts errors into `gdb.GdbError`, whose
  message GDB prints cleanly; an explicit `gdb.GdbError` from a body passes
  through unchanged. Adapting to a missing object is *not* an error and still
  returns a null `gdb.Value`.
- **Probe helpers.** Bridge primitives (`safe_int`, `safe_dereference`,
  `value_address`, DWARF `_fields`), and GDB tab completion degrade to safe
  defaults (`None`, `0`, `[]`). These are the building blocks guards rely on.
  They catch only *expected* types (`gdb.error`, `gdb.MemoryError`,
  `IndexError`, `TypeError`, `ValueError`, `AttributeError`); anything
  unexpected now bubbles to a guard for a full diagnostic instead of being
  silently swallowed. Completion is a notable exception: GDB completion must
  never print or raise, so `_object_names` keeps a deliberate broad catch that
  degrades to no candidates.

The guard's reporting policy:

- `gdb.error` / `gdb.MemoryError` → `warn()` (recoverable, continues);
- any other `Exception` → `err()` one-liner, or a full
  `show_last_exception()` diagnostic when `GDR_DEBUG` is enabled;
- `GDR_PROPAGATE_EXCEPTION` additionally re-raises after a debug diagnostic
  so a reported bug can be surfaced or tested.

### Derived data and state for debugging targets

List and detail output supplement raw kernel fields with derived state that
has direct diagnostic value, always computed from the target's own structures
rather than hardcoded assumptions:

- **Field Symbolization** dereference address, flag or bit mask value to
  meaningful symbol as possible. Otherwise roll back to hex number regard
  gdb output-radix setting.
- **IPC waiter summaries** are derived by traversing each suspend list
  (`count@names`), never from cached counters that's unstable over kernels.
  A version without a sender wait list renders `N/A`, never a fabricated `0`.
- **Capacity and policy columns** derive from kernel counters and object
  flags: `Free = capacity - entry`, `Used = total - free`, and a
  `FIFO`/`PRIO` policy decoded from the IPC flag.
- **Detail diagnostics** extend the list columns with bounded, corruption-
  guarded raw-memory walks and consistency verdicts (event waiter
  `event_set`/mode pairing, mailbox FIFO offset check, message-queue chain
  counts, memory-pool free-list and alignment checks, timer wrap-safe
  `ExpiresIn`).

### Width-adaptive lists

`rtos <objects>` tables adapt to the current terminal width without ever
changing their column set. The effective width is probed in priority order
(`set width`, `shutil.get_terminal_size`, fallback 120) in
`gdb.gdb_bridge.terminal_width`, while pure formatting lives in
`gdr.formatting.format_table` so unit tests can pin 80/100/120/160 columns.
When the natural table is too wide, only text columns explicitly marked
`elastic` by the adapter shrink, in adapter-provided priority order, and
overlong cells truncate with `..` while preserving a leading waiter count. If
even the minimum elastic widths overflow, the natural table is printed
unchanged and the terminal may wrap it. Numeric, state, and address cells are
never truncated or dropped.

### Critical vs non-critical field placement

Kernel fields are classified by diagnostic value, and that classification
fixes where each field may appear:

- **Critical fields** (waiter counts, `Addr`, `ExpiresIn`, `OrigPrio`,
  SMP `CPU`/`Bind`) are mandatory in list output and survive any width
  adaptation; their meaning must never be silently truncated away.
- **Non-critical single-value fields** (`Policy`, `Free`, `Used`) stay in the
  compact list when the 120-column budget allows, without adding rows.
- **Non-critical detail fields** (internal pointers, per-waiter conditions,
  consistency checks, thread `error`/`remaining_tick`) appear only in the
  singular `rtt <object> <name>` detail, or via native `$gdr_object()` /
  GDB expressions.

This mirrors the field taxonomy described earlier in this document: the list
column set is stable, and deep or verbose state is reached through the detail
command or raw `gdb.Value` inspection rather than by widening the default
table.

## Runtime data flows

Initialization selects the adapter and builds its layout before registration:

```text
gdr init -> RTOS version policy -> config probes/layout -> adapter
         -> printers/functions/commands registration
```

Aggregate commands use the adapter-owned presentation path:

```text
rtt/freertos command -> generic coordinator -> active adapter
  -> RTOS navigation/layout -> adapter intermediate model
  -> ObjectTable/ObjectDetail/SystemSummary
  -> shared formatting -> GDB output
```

Native inspection stays separate:

```text
$gdr_task/$gdr_tasks/$gdr_object -> active adapter -> pure RTOS navigation
  -> raw gdb.Value or target-native pointer array
GDB p/bt/info -> registered printer -> active layout metadata -> GDB display
```

## Closed-loop verification

GDR maybe degrade silently after update: the script runs but output is wrong.
To guard against this, QEMU smoke tests boot RTOS-specific firmware that
creates known objects and assert, where an adapter implementation exists:

- pretty-printers registered and fold correctly,
- convenience functions return non-null `gdb.Value` with expected fields,
- aggregate commands list the expected objects.

The FreeRTOS B-L475E-IOT01A fixture asserts ready-marker delivery, retained
DWARF for kernel structures, the 32-bit ABI, persistent GDB, scheduler-list
navigation, current-task marking, system counters, and pretty-printer fold
for typedef-spelled kernel objects (Task/List/Queue). The queue family adds
ground-truth assertions against the objects the fixture creates: the queue
lengths and item counts, the blocked sender/receiver names in `SendWait`/
`RecvWait`, the semaphore count/max, the mutex `Held`/`Owner`/`Recursive`
cells (including a two-level recursive take), the FIFO `Item[0]` byte dump,
the absence of any owner field in a semaphore detail, the `Set` column
appearing only on the queue-set variant, and the `?` inference marker on the
trace-off variant.

### Test infrastructure

Tests use a **persistent GDB session** driven by `pexpect`:

1. A session-scoped `QemuSession` starts the selected QEMU profile with a
   dynamically allocated `-gdb tcp::<port>` endpoint (free-running, no `-S`)
   and waits for the profile's ready marker. Every session owns distinct serial
   and QEMU-output logs, which are included in boot timeout and early-exit
   diagnostics.
2. A session-scoped `GdbSession` spawns one GDB process via `pexpect`,
   connects to QEMU, and runs `source gdr.py` **once**. All tests in the
   suite reuse this single GDB connection, keeping convenience
   functions and pretty-printers registered across tests.
3. Each test calls `gdb_session.run(...)` to execute a GDB command and capture
   output. ANSI escape sequences and PTY artifacts are stripped automatically.

This approach (borrowed from `pytest-embedded-jtag`'s `Gdb` class) is
preferred over spawning a fresh GDB batch process per test: it is faster
and avoids registration-state loss between tests.

`tests/integration/conftest.py` is the pytest assembly layer: it selects
profiles and constructs session fixtures. Reusable QEMU/GDB process lifecycle,
dynamic ports, logs and timeout diagnostics live in
`tests/support/qemu_harness.py`; the two files intentionally remain separate.

### Target profiles

`GDR_QEMU_TARGET` selects the profile while keeping all GDR assertions shared:

| Target | QEMU startup | GDB symbols | Notes |
| -------- | -------------- | ------------- | ------- |
| `cortex-a9` | `qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` | `rtthread.elf` | No SD device is required for the fixture boot path. |
| `rv64` | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin` | `rtthread.elf` | M-Mode boot, no SD image, `set architecture riscv:rv64`. |
| `b-l475e-iot01a` | `qemu-system-arm -M b-l475e-iot01a -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | FreeRTOS V10.3.1 Cortex-M4F SysTick fixture, 32-bit pointers. Variant selected by `GDR_FIXTURE_VARIANT`. |
| `mps2-an385` | `qemu-system-arm -M mps2-an385 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | FreeRTOS-Kernel tag builds (10.4.x / 10.5.x / 11.1.x) on Cortex-M3. |

The ELF and firmware image may be separate: RV64 deliberately boots a raw BIN
while GDB requires the DWARF ELF. The shared suite asserts each profile's
pointer width, including `sizeof(void *) == 8` for RV64 and 4 for the FreeRTOS
Cortex-M fixture.

`tests/support/rtthread_fixture_profiles.py` separately owns fixture-level expectations that
vary by target or RT-Thread version: object enum values, the current-thread
expression, and canonical fixture object names. It intentionally does not
import production layout metadata, so a regression in GDR's layout mapping
cannot update the expected values at the same time. Cortex-A9 fixture patches
set `RT_NAME_MAX` to 16, preserving the canonical `test_mutex` and
`test_timer` names used by every shared test; the RV64 BSPs already use 20.

The RV64 matrix covers RT-Thread v4.0.4, v4.0.5, v4.1.0, and v4.1.1. The BSP
is `bsp/qemu-riscv-virt64` through v4.1.0 and is renamed to
`bsp/qemu-virt64-riscv` in v4.1.1; each path has a separate platform-specific
patch set.

Layout changes must update the corresponding assertion, keeping the
helper and the kernel struct in lockstep.

The Cortex-A9 matrix covers RT-Thread 3.1.0 through 3.1.5 as well as the 4.x
representatives. It runs the complete suite at 3.1.0, 3.1.3, and 3.1.5, which
cover the pre-`Null` object enum, the enum/`rt_semaphore` transition, and the
final 3.1 layout. The remaining 3.1 tags retain build coverage for their exact
Cortex-A9 fixtures. Upstream has no QEMU RV64 BSP for 3.1.x, so that range is
Cortex-A9 only.

### Known constraints

**Fixture-first.** Work may consume a config branch (SMP, heap_N, static
allocation, stream buffers, MPU, runtime stats, queue sets, …) only when a
live firmware variant or a static snapshot can falsify it. Branches without
a fixture stay deferred unit-test stubs, never "done".

**FreeRTOS live coverage is 32-bit Cortex-M only.** Every FreeRTOS lane is a
32-bit target (`b-l475e-iot01a` Cortex-M4F, `mps2-an385` Cortex-M3, the
Cortex-M33 static snapshot). Pointer and field widths are always taken from
DWARF (`read_path` reads each union arm at its target type, and the reserved
MPU-pool handle is compared at the target pointer width), so no literal width
constant exists in the adapter — but that property is unverified on a 64-bit
target until a 64-bit lane exists.

**FreeRTOS version detection depends on `-g3` macro debug info and CU scope.**
`detect_target_version()` reads the `tskKERNEL_VERSION_*` macros via
identifier eval and `info macro`. Both consult the current compilation unit's
`.debug_macro` first; `info macro -a` plus the fixture-exported
`gdr_freertos_version_num` cover a halt in HAL / startup / application CUs.
A remaining miss degrades to a warning ("target FreeRTOS version is not
exported") and skips the mismatch check rather than guessing.

**FreeRTOS live fixtures are a variant matrix**, not a single configuration.
`ci/freertos/fixture/config/<variant>/` plus a shared `main.c` produce
`base` (the historical B-L475E-IOT01A / 10.3.1 combination), `full`,
`static-only`, `static-dynamic`, `trace-off`, `heap-1`/`2`/`3`/`5`,
`heap-protector` (≥V11), and `registry-0`. Cache layout is
`<cache>/<target>/<version>/<variant>/freertos.elf`. Independent expected
capabilities live in `tests/support/freertos_fixture_profiles.py` and must not
be derived from `freertos/layout.py`.

The historical `base` combination remains: single-core / Cortex-M4F / heap_4
/ trace_facility=on / FreeRTOS 10.3.1 / `configENABLE_BACKWARD_COMPATIBILITY=1`
(member name `pvContainer`) / no `pxEndOfStack` / scalar `ucNotifyState` /
no runtime statistics / `configMAX_PRIORITIES=6` / stack grows down. Default
builds still use `pvContainer`; without `pxEndOfStack`, `Stack`/`Used` stay
N/A and HighWater scans `[pxStack, pxTopOfStack)`.

**Not covered by any current fixture (documented, unit-tested only):**

- `mpu-pool` (`portUSING_MPU_WRAPPERS` + MPU wrappers v2 / `xKernelObjectPool`):
  needs an MPU port (`portable/GCC/ARM_CM33` or `ARM_CM33_NTZ`) plus a fixture
  built on `xTaskCreateRestricted` and the `MPU_` wrapper API. TrustZone is not
  required (`NTZ` means "no TrustZone"; QEMU does model the ARMv8-M security
  extension on `mps2-an505`/`an521`/`musca-*`) -- the blocker is simply that no
  such build has been booted here yet.
- `stack_grows_up`: **not supported**. GDR decodes stacks as grow-down only
  (high water mark scans the untouched fill from the low end). The sole
  upstream `portSTACK_GROWTH +1` port is SDCC/Cygnal 8051, which has no GCC
  toolchain and no QEMU machine, so no grow-up target can exist; the dead
  field/branch was removed rather than kept as an untestable probe.
- Timer daemon queue `pended callback` arm (`xMessageID < 0`): the arm only
  exists when `INCLUDE_xTimerPendFunctionCall == 1` (timers.c/timers.h), and
  no current fixture variant defines that macro. The queue decoder therefore
  gates on the presence of the `u.xCallbackParameters` DWARF member and
  renders a missing arm as `pended-callback arm absent
  (INCLUDE_xTimerPendFunctionCall=0)` instead of decoding garbage; a config
  variant that enables the macro and leaves a negative-id slot in the queue
  is fixture-pending.
- Event-group control bits are only exercised at 32-bit tick width: every
  FreeRTOS lane is 32-bit (`EventBits_t == TickType_t`), so the 16/64-bit
  mask derivation (`event_bit_masks`) and the width-agnostic decode are
  verified by unit tests only.
- Stream/message buffer **ring wrap** (`xHead < xTail`) has no live evidence:
  the fixture never sends to its stream/message/batching buffers
  (`xStreamBufferSend`/`xMessageBufferSend` are never called), so Bytes is
  always 0 and the wrap branch of `bytes_in_buffer`/`spaces_available` is
  unit-test-only. The batching `>` trigger asymmetry is likewise unobservable
  while the batching buffer is empty, and the `xLength == 0 && pucBuffer ==
  NULL` deleted-buffer signature has no fixture (no `vStreamBufferDelete`
  call) -- all three are documented as unit-tested only. The same empty-buffer
  fact keeps `NextMsg` on the unit-test-only list: the length prefix is read at
  `pucBuffer + xTail` with the kernel's two-part wrap, but no fixture ever puts
  a message in a message buffer, so live rows always print `-`.
- Event-group `ucStaticallyAllocated` is gated by the value of the field only
  in unit tests. The *presence* of the member is live-verified on two lanes
  (`ptype struct EventGroupDef_t` has it on `static-dynamic`, not on `base`,
  because the kernel declares it only when static *and* dynamic allocation are
  both enabled), but the `static-dynamic` variant sets
  `GDR_FIXTURE_MIXED_ALLOCATION`, so its "static" event group is in fact
  created dynamically and no live object reports `StaticallyAllocated: yes`.
- `ListIntegrityBytes` is `skipped` on every fixture. No build defines
  `configUSE_LIST_DATA_INTEGRITY_CHECK_BYTES`, so
  `xListItemIntegrityValue1`/`2` do not exist and `cfg.list_integrity_check`
  is false everywhere; the magic derivation (`0x5a5a` / `0x5a5a5a5a` /
  `0x5a5a5a5a5a5a5a5a` from the tick width, `include/projdefs.h`) is
  unit-tested only. A live kernel cannot supply the negative either: the
  integrity bytes are only ever validated by `configASSERT` inside
  `vListInsert`, so a corrupted value crashes the target instead of being
  observable. Enabling the macro on the static snapshot adds two `TickType_t`
  members to `List_t`/`ListItem_t` and therefore rewrites every list and TCB
  initializer in the snapshot; that is the fixture work this check waits on.

**Static snapshot lane** (`ci/freertos/build-fixture-snapshot.sh` with sources
in `ci/freertos/snapshot/`) covers states a healthy kernel cannot produce
(corrupt lists, SMP `xTaskRunState` of `0`/`1`/`-1`/`-2` without a live
dual-core port, a timer on the overflow list, and corrupt-heap cells). Snapshot
data is non-zero-initialized into `.data`; file-only GDB synthesises zeros for
`.bss`, so a BSS-resident structure would read back as a successful decode of
empty lists (see `ci/freertos/README.md`). Zero-valued standalone globals are
forced into `.data` with `__attribute__((section(".data")))` for the same
reason. The heap *mismatch* cell (free-list vs linear vs counter disagree) and
the *allocated-bit* cell (free-list member with the size_t MSB) cannot share
one heap symbol set — a corrupt walk swallows the mismatch verdict — so the
allocated-bit cell lives in a second, heap-only ELF (`snapshot_heap.c`).

**FreeRTOS does not provide an `Entry` column.** The FreeRTOS TCB
(`tskTaskControlBlock`) does not store the task entry function pointer after
task creation; it exists only transiently on the initial stack frame and is
overwritten on first context switch. This is a fundamental difference from
RT-Thread's `rt_thread.entry`, which persists in the TCB. The `frt help`
output documents this limitation.

**FreeRTOS high-water mark semantics.** The `HighWater` column reports the
number of `StackType_t` words that have never been overwritten (matching
`uxTaskGetStackHighWaterMark` semantics). `unavailable` means either the stack
was never filled with `0xa5` or the distinction between "unfilled" and
"completely exhausted" cannot be made without an independent evidence source.
The scan window is `[pxStack, pxTopOfStack)` (no `pxEndOfStack` required).

**Queue `type` field is gated by `trace_facility`.** The `ucQueueType` member
only exists under `configUSE_TRACE_FACILITY == 1`. Layout summary fields that
reference config-conditional struct members must be gated by the corresponding
`FreeRtosConfig` flag; unconditional inclusion produces `N/A` on builds where
the member is absent.

**FreeRTOS heap blocks carry no owner field, so per-task heap usage is not
attributable.** A `BlockLink_t` is exactly `{pxNextFreeBlock, xBlockSize}`
(heap_4.c) with no owner member; `frt heap` therefore never offers a thread-occupancy
breakdown, and `frt help` documents this instead of faking a column. `frt heap`
renders the `Algorithm/TotalSize/FreeSize/MinEver/Allocs/Frees/Protector/Blocks/
Holes/CrossCheck` ten-key order on every kind, writing `unavailable` for keys the
kind has no symbol for, so the column never drifts between variants.

**heap_5 without `configENABLE_HEAP_PROTECTOR` has no linear-walk bounds.**
The kernel exports no region bases without the protector (`pucHeapLowAddress`/
`pucHeapHighAddress` only exist then, heap_5.c), the intermediate region-markers
can be unlinked by forward coalescing, and the region table is a function-local
array — so `frt heap` skips the linear walk, renders `Blocks` from the free-list
walk only and reports `CrossCheck: unavailable: heap_5 region bases unknown`.
With the protector a single-region heap can be walked from its base to `pxEnd`;
no live fixture exercises that branch (the `heap-protector` variant links
heap_4), so it is unit-tested only.

**The heap_4/heap_2 linear walk starts at `align_up(&ucHeap)`, never at the
free-list head.** `pvPortMalloc` carves every allocation from the *front* of the
first suitable free block and puts the remainder back in the list (heap_4.c:261,
278-288), so after the first allocation the free-list head sits above the
allocated blocks at the heap base; heap_2 additionally sorts its free list by
size, so its head has no relation to the physical base at all. Walking from the
head therefore silently drops every block below it while the cross-check — which
compares only free bytes and free-block addresses, all at or above the head —
still reports `ok`. GDR walks from the kernel's own `pucAlignedHeap`
(`align_up(&ucHeap, portBYTE_ALIGNMENT)`); when the `ucHeap` symbol cannot be
resolved the walk is skipped (`Holes`/`CrossCheck` become `unavailable`) rather
than truncated from the head. Live evidence on `b-l475e-iot01a/10.3.1/base`: the
aligned base is 13792 bytes below `xStart.pxNextFreeBlock`, and the walk from the
base covers 57 blocks (56 allocated, 1 free) up to `pxEnd`, summing to exactly
`xFreeBytesRemaining`.

**An uninitialised heap has no derivable used bytes.** Before `prvHeapInit` runs,
`xFreeBytesRemaining` still holds its static initializer (0 for heap_4), so
`total - free` would report the whole heap as used; `frt system` omits the
`Heap used` line entirely in that state and keeps only the statically knowable
`Heap total`. heap_2's initialisation flag is not always readable either: at
`-Og` the fixtures fold `xHeapHasBeenInitialised` into a non-debug local symbol,
so the state falls back to `xEnd.xBlockSize` (written by `prvHeapInit`, zero
before it).

**Live heap coverage is 32-bit Cortex-M only.** Every FreeRTOS lane has a 32-bit
`size_t` and `portBYTE_ALIGNMENT == 8`, so the 64-bit allocation-mask arithmetic
is unit-tested only. The cross-check `mismatch` and walk `corrupt` verdicts
were historically unit-tested only too (a healthy kernel cannot produce a
corrupt heap); the static snapshot lane now carries crafted `mismatch` (free-list
skips a linear-free block) and allocated-bit (free-list member with the size_t
MSB) cells, so those verdicts have live evidence.

**Object discovery is a six-channel provenance model, not a registry walk.**
FreeRTOS keeps no global object registry for most kinds (only the optional
`xQueueRegistry` and the MPU wrappers v2 pool), so GDR discovers objects from
six channels, each of which attaches its origin to the object:

| channel | source | covers |
| --- | --- | --- |
| MPU pool | `xKernelObjectPool` (file-static, wrappers v2 only) | all kinds |
| registry | `xQueueRegistry`, whole array scanned (holes included) | queue/semaphore/mutex |
| active | `pxCurrentTimerList` / `pxOverflowTimerList` | timers (active only) |
| symbol | one `info variables` scan per session, cached; `Static*_t` buffers use the symbol address, `*Handle_t` uses the pointer value | all kinds |
| waiter | reverse `container_of` from a blocked TCB's `xEventListItem.pxContainer`, confirmed by list membership and struct plausibility | queue/semaphore/mutex/event group |
| user | explicit `0x`/decimal address or symbol name | all kinds |

`discover(kind)` merges the channels in that priority order, deduplicating by
address: the first channel to find an address keeps its name and stays the
source of record, and every later channel that finds the same address is
recorded as an extra source. The `Src` cell therefore shows the full evidence
(`registry+symbol+waiter`), because corroboration by several channels is
stronger evidence than a single heuristic hit — which matters exactly when a
name or kind looks wrong. The `frt objects` summary still counts each object
under its source of record only, so the per-source counts keep summing to the
kind's total; it prints that `source=count` breakdown plus the enumeration
limitation, so a count is never mistaken for a complete inventory:
unregistered dynamic objects without a global handle (and stopped or expired
timers) are simply not reachable from any channel.

The consistency checks stay exhaustive but their *rendering* is split by what
the reader can act on: the `Checks` row carries a verdict plus counts
(`ok (4 verified, 2 n/a)`), a check that is inapplicable by structure (a mutex
has no storage pointers) is counted rather than named, and only failures or
unreadable fields get their own `Check[<name>]` row. Enumerating every skip
inline produced a 150-column value that buried the one line worth reading.
`frt system` reaches the same rendering through `SystemSummary.extra_pairs`,
an adapter-owned list of vertical rows the neutral renderer appends verbatim,
so the RTOS-specific verdict strings never enter `gdr/`.

A singular detail command refuses a kind it can prove wrong: resolving a name
or address stamps the *requested* kind on whatever it found, so
`frt semaphore <a mutex>` used to render a semaphore block whose every
consistency check failed. The resolved object is refined through the `Queue_t`
discriminator and, when the actual kind is known and different, the command
replies with a redirect (`'gdr_mutex' is a mutex, not a semaphore; try
`freertos mutex gdr_mutex``). An *inferred* kind never justifies a refusal —
without `configUSE_TRACE_FACILITY` the discriminator cannot tell a binary from
a counting semaphore, so the detail renders with the usual `?` marker.

Two deliberate heuristics: the registry and MPU-pool queue slots only prove "some
`Queue_t`", so the discovery layer refines them through the `Queue_t`
discriminator — with `configUSE_TRACE_FACILITY` the kind is definitive, without
it only the family is known and `inferred_kind` stays set (rendered with a `?`).
The waiter channel validates hosts by field plausibility (`uxLength`/`uxMessagesWaiting`/
`uxItemSize`, non-null `pcHead` for item queues, and the
`xListEnd.xItemValue == portMAX_DELAY` sentinel on both waiting lists), which
can in principle still misread plausible neighbouring memory — every other
channel outranks it.

The `mpu-pool` channel is implemented with unit-test stubs only: no live
fixture exists (it needs a CM33 MPU port and a restricted-task fixture, not
TrustZone), so its behaviour has no
live coverage. The `waiter` channel is pinned by a deliberate fixture object:
`gdr_waiter_only_eg_task` (ci/freertos/fixture/main.c) blocks forever on an
event group whose handle lives only on its own stack, so that group is
discoverable *only* through the waiter channel -- the live no-ghost assertion
now permits exactly one host (that anonymous event group) that no earlier
channel finds, and would fail if the container_of probe fabricated objects out
of plausible neighbouring memory beyond it.
