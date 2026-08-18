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
|--------|---------------|
| `gdb_bridge.py` | Wraps GDB registration, safe eval, memory/type access, terminal probing, output and error guards. |
| `constants.py` / `formatting.py` | Shared traversal/presentation defaults and pure optional/address/symbol/table formatting. Formatting has no GDB dependency. |
| `layout.py` | Generic `StructLayout` / `StructField` / `ListHook` dataclasses and accessors (`read_field`, `iter_list`, `container_of`). It interprets adapter-supplied paths but contains no target type names, symbols, or list conventions. |
| `printers.py` | Generic pretty-printer registration and rendering. Display labels, summary fields, enum maps, and pointee display paths come from the adapter layout. |
| `version.py` | RTOS-neutral version parsing, range checks, formatting and declared decimal/packed-hex decoding. |
| `adapter_api.py` | `RtosAdapter`, `ObjectTable`, `ObjectDetail`, `SystemSummary`, and the single active adapter selected by `gdr init`. |
| `commands.py` / `functions.py` | Generic output coordination and raw-value convenience functions; task columns and object vocabulary remain adapter-owned. |

### `rtthread/` — adapter

| Module | Responsibility |
|--------|---------------|
| `layout.py` | **The only place that knows RT-Thread struct layouts.** Defines `RtConfig`, `detect_config()` (symbol-presence probing), and `build_layouts(config) -> KernelLayout`. Handles config-conditional fields (SMP, heap manager, IPC toggles) via factory branches, not version-branched files. |
| `navigation.py` | RT-Thread object navigation: registry/current-thread/tick entry symbols, type codes, and timer traversal. Returns raw `gdb.Value` objects using the layouts supplied by `layout.py`. |
| `adapter.py` | RT-Thread intermediate object models, `gdb.Value` conversion, adapter-owned task/object tables, detail dispatch and system summaries. These models are presentation inputs, not ABI layout descriptions. |
| `diagnostics.py` | Consumes adapter intermediate models for detail rendering and performs bounded raw mailbox/message-queue/mempool walks and consistency diagnostics. |
| `version.py` | RT-Thread support ranges, exported target symbols, encoding order and RT-Thread-specific diagnostics. |
| `commands.py` | The `rtthread` / `rtt` command tree, including routing, aliases, and `rtt help`. |

### `freertos/` — adapter

| Module | Responsibility |
|--------|---------------|
| `layout.py` | FreeRTOS config/DWARF probes, logical struct paths, `FreeRtosLayout`, and the complete `FreeRtosTask`-related capability metadata. Config and layout stay together because the detected TCB fields directly determine the built paths. |
| `navigation.py` | Pure scheduler-list and current-task traversal functions. List member access uses logical `end`/`next`/`owner`/`count` fields from `FreeRtosLayout`; walks are bounded and corruption-guarded. |
| `adapter.py` | The complete `FreeRtosTask` intermediate model, TCB conversion, adapter-owned task columns, and system summary. |
| `version.py` | FreeRTOS support ranges, exported target symbols, encoding order and FreeRTOS-specific diagnostics. |
| `commands.py` | The `freertos` / `frt` task and system command tree. Queue/timer object commands are not currently implemented. |

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
warning when a symbol is ambiguous.

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

### `gdb_command_guard` for kernel data collection

RTOS debugging routinely touches target memory through GDB, and any read can
raise `gdb.error` / `gdb.MemoryError` (target halted, unmapped memory, remote
link dropped). Without a guard these bubble up as GDB "Python Exception"
noise and abort the rest of the command. Every command body in `gdr/commands.py`
is therefore wrapped with `@gdb_command_guard`, which:

- converts `gdb.error` / `gdb.MemoryError` into a `warn()` (recoverable, the
  command reports and continues);
- converts any other `Exception` into `err()`, including a full traceback
  only when `GDR_DEBUG` is set.

This keeps derived-data collection resilient: one bad read degrades to a
stable one-line diagnostic instead of aborting the whole command tree.

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

GDB helpers degrade silently: the script runs but output is wrong. To
guard against this, QEMU smoke tests boot RTOS-specific firmware that creates
known objects and assert, where an adapter implementation exists:

- pretty-printers registered and fold correctly,
- convenience functions return non-null `gdb.Value` with expected fields,
- aggregate commands list the expected objects.

The FreeRTOS B-L475E-IOT01A fixture asserts ready-marker delivery, retained
DWARF for kernel structures, the 32-bit ABI, persistent GDB, scheduler-list
navigation, current-task marking, and system counters. Pretty-printers and
queue/timer object commands are not current FreeRTOS adapter capabilities.

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
|--------|--------------|-------------|-------|
| `cortex-a9` | `qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` | `rtthread.elf` | No SD device is required for the fixture boot path. |
| `rv64` | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin` | `rtthread.elf` | M-Mode boot, no SD image, `set architecture riscv:rv64`. |
| `b-l475e-iot01a` | `qemu-system-arm -M b-l475e-iot01a -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | FreeRTOS V10.3.1 Cortex-M4F SysTick fixture, 32-bit pointers. |

The ELF and firmware image may be separate: RV64 deliberately boots a raw BIN
while GDB requires the DWARF ELF. The shared suite asserts each profile's
pointer width, including `sizeof(void *) == 8` for RV64 and 4 for the FreeRTOS
Cortex-M fixture.

`tests/support/rtthread_profiles.py` separately owns fixture-level expectations that
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
