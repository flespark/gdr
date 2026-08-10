# Architecture

## Goals

Reduce the cognitive load of debugging complex RTOS-based embedded
firmware in GDB by:

- Folding noisy wrapper-type output (pretty-printers).
- Solving object navigation once (convenience functions).
- Aggregating multi-object state into tables (commands).

Non-goals: replacing GDB expressions, wrapping QEMU monitor commands, or
duplicating what `rust-gdb` / `gdb` already display well.

## Layering

```text
                     gdr.py  (entry: arg parse, bootstrap, register)
                        |
        +---------------+----------------+
        |                                |
       gdr/  (RTOS-agnostic core)      rtthread/  (RT-Thread 3.1.x/4.x adapter)
         |                                |
    gdb_bridge.py                   layout.py
    layout.py                       navigation.py
    printers.py                     adapter.py
    abstractions.py                 commands.py
```

### `gdr/` — core

| Module | Responsibility |
|--------|---------------|
| `gdb_bridge.py` | Wraps the GDB Python API: command/function registration, table printing, safe eval, error guards. Keeps `gdb.*` calls in one place so the rest of the code is testable. |
| `layout.py` | Generic `StructLayout` / `StructField` / `ListHook` dataclasses and accessors (`read_field`, `iter_list`, `container_of`). It interprets adapter-supplied paths but contains no target type names, symbols, or list conventions. |
| `printers.py` | Generic pretty-printer registration and rendering. Display labels, summary fields, enum maps, and pointee display paths come from the adapter layout. |
| `abstractions.py` | Neutral table-output dataclasses (`Thread`, `Semaphore`, `Mutex`, `Timer`, and related objects). They are not used to replace raw `gdb.Value` navigation results. |

### `rtthread/` — adapter

| Module | Responsibility |
|--------|---------------|
| `layout.py` | **The only place that knows RT-Thread struct layouts.** Defines `RtConfig`, `detect_config()` (symbol-presence probing), and `build_layouts(config) -> KernelLayout`. Handles config-conditional fields (SMP, heap manager, IPC toggles) via factory branches, not version-branched files. |
| `navigation.py` | RT-Thread object navigation: registry/current-thread/tick entry symbols, type codes, and timer traversal. Returns raw `gdb.Value` objects using the layouts supplied by `layout.py`. |
| `adapter.py` | Value→dataclass converters (`value_to_thread`, `value_to_semaphore`, …) and `gdb.Function` subclasses (`$gdr_thread`, `$gdr_threads`, `$gdr_object`). The `_value_to_str()` helper handles GDB string literals whose `type.code` is `TYPE_CODE_ARRAY`, not `TYPE_CODE_STRING`. |
| `commands.py` | The 5 aggregate commands. Argument parsing + table output only; no struct knowledge. |

## Key decisions

### No RTOS / version auto-detection

Previous versions attempted to detect the RTOS and parse its version string
from symbols, then match struct patterns. This was fragile (failed on
attach, failed across remote configs) and duplicated logic. Users now
specify the RTOS and exact version, for example
`gdr init rtthread 4.0.5`. The RT-Thread adapter validates the explicitly
verified `3.1.0-3.1.5` and `4.0.0-4.1.1` intervals, while layout differences
are still handled by probing target symbols and DWARF rather than branching on
every patch version. The sole retained version-level layout branch is the
RT-Thread 3.1.3 insertion of `Null = 0` in `rt_object_class_type`: it shifts
every object type code, so the active `KernelLayout` owns the semantic-name to
numeric-code mapping. RT-Thread 3.1 thread object flags are likewise mapped to
their actual `flags` member.

### Config features are probed, not specified

RT-Thread kernels vary by *configuration* far more than by version:
`RT_USING_SMP` adds `oncpu` to `rt_thread`; the heap manager
(`small_mem` / `slab` / `memheap`) changes the heap data structures
entirely; IPC components (`RT_USING_MUTEX`, etc.) may be absent.
Probing these by symbol presence (`rt_cpu_index`, `rt_sem_init`,
`rt_mutex_take`, ...) is reliable and cheap, and spares users from
reciting their `.config`. Probing falls back to safe defaults with a
warning when a symbol is ambiguous.

### Wrapper types first, per adapter

"Wrapper-first" is an adapter-level prioritisation rule, not a global list
of types. An adapter should first identify values whose default GDB display
is dominated by implementation detail before the logical value: wrappers,
handles, synchronisation objects, references, or other frequently inspected
implementation-heavy types. The relevant types depend on the RTOS, source
language, toolchain, but printers already supplied by GDB. An adapter need
not add a printer when native GDB output is already useful.

The core does not identify wrapper types or prescribe their type names,
labels, or field paths. `gdr.printers` renders only metadata supplied by the
active adapter, letting each target improve `p`, `bt full`, `info locals`, and
watchpoint output without leaking one RTOS's type taxonomy into another.

"Wrapper-first" describes priority, not scope: it does not request another
Python model around every kernel object. Convenience functions solve target-
specific navigation and return raw `gdb.Value` objects or GDB-native
collections of raw pointers. GDB expressions and pretty-printers remain
responsible for inspecting and presenting those values. Commands own display
aggregation -- tables, trees, and derived summaries -- for collections that
are awkward to express in GDB.

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

FreeRTOS has an independent adapter under `freertos/`. Phase 2 owns version and
configuration probes, DWARF-path layouts, scheduler-list navigation, task
conversion, and the `freertos tasks/system` commands. Queue and timer object
enumeration is intentionally deferred to Phase 3; the core `gdr/` package
remains generic throughout.

### Coupling is explicit and localised

All RT-Thread coupling lives under `rtthread/`: `layout.py` owns field paths,
type names, display metadata, and state encodings; `navigation.py` owns
RT-Thread symbols and registry traversal. When an RT-Thread struct or entry
point changes, those files — plus their QEMU smoke-test assertions — are the
review surface. This mirrors the Asterinas `constants.py` + `COUPLED`
annotation discipline. `gdr.py` is the intentional composition root that
selects an adapter; modules inside `gdr/` never import or identify one.

### Commands only aggregate

Per the Asterinas experience: commands should provide the multi-object
presentation that GDB expressions cannot conveniently produce: tables, trees,
and derived summaries. Convenience functions may navigate a collection, but
leave element inspection to native GDB expressions. Single-object field
inspection is left to `$gdr_thread(name)` + `p $gdr_thread(name).field`. This
keeps the command set small and avoids commands silently breaking when a field
is renamed (the function returns the raw `gdb.Value`).

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
queue/timer object commands remain later adapter phases.

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

### Target profiles

`GDR_QEMU_TARGET` selects the profile while keeping all GDR assertions shared:

| Target | QEMU startup | GDB symbols | Notes |
|--------|--------------|-------------|-------|
| `cortex-a9` | `qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` | `rtthread.elf` | No SD device is required for the fixture boot path. |
| `rv64` | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin` | `rtthread.elf` | M-Mode boot, no SD image, `set architecture riscv:rv64`. |
| `b-l475e-iot01a` | `qemu-system-arm -M b-l475e-iot01a -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | FreeRTOS Phase 1, Cortex-M4F SysTick fixture, 32-bit pointers. |

The ELF and firmware image may be separate: RV64 deliberately boots a raw BIN
while GDB requires the DWARF ELF. The shared suite asserts each profile's
pointer width, including `sizeof(void *) == 8` for RV64 and 4 for the FreeRTOS
Cortex-M fixture.

`tests/rtthread_profiles.py` separately owns fixture-level expectations that
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
