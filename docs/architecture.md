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
       gdr/  (RTOS-agnostic core)      rtthread/  (RT-Thread v4.x adapter)
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
`gdr init rtthread 4.0.5`. The RT-Thread adapter validates the
supported 4.x.x range, while layout differences are still handled by probing
target symbols and DWARF rather than branching on every patch version.

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
specific navigation and return raw `gdb.Value`; GDB expressions and
pretty-printers remain responsible for inspection and presentation. Commands
only aggregate collections that are awkward to express in GDB.

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

If a second RTOS (e.g. FreeRTOS) is added later, it gets its own
`freertos/` adapter package with its own layout and navigation modules; the
core `gdr/` package stays generic.

### Coupling is explicit and localised

All RT-Thread coupling lives under `rtthread/`: `layout.py` owns field paths,
type names, display metadata, and state encodings; `navigation.py` owns
RT-Thread symbols and registry traversal. When an RT-Thread struct or entry
point changes, those files — plus their QEMU smoke-test assertions — are the
review surface. This mirrors the Asterinas `constants.py` + `COUPLED`
annotation discipline. `gdr.py` is the intentional composition root that
selects an adapter; modules inside `gdr/` never import or identify one.

### Commands only aggregate

Per the Asterinas experience: commands should do what GDB expressions
cannot — iterate collections, tabulate, build trees. Single-object
field inspection is left to `$gdr_thread(name)` + `p (*.thr).field`.
This keeps the command set small and avoids commands silently breaking
when a field is renamed (the function returns the raw `gdb.Value`).

## Closed-loop verification

GDB helpers degrade silently: the script runs but output is wrong. To
guard against this, QEMU smoke tests boot an RT-Thread v4.x firmware
that creates known threads/semaphores/mutexes/timers, and assert:

- pretty-printers registered and fold correctly,
- convenience functions return non-null `gdb.Value` with expected fields,
- aggregate commands list the expected objects.

### Test infrastructure

Tests use a **persistent GDB session** driven by `pexpect`:

1. A session-scoped `QemuSession` starts the selected QEMU profile with
   `-gdb tcp::1234` (free-running, no `-S`) and waits for the fixture's
   `GDR test fixture ready.` serial marker.
2. A session-scoped `GdbSession` spawns one GDB process via `pexpect`,
   connects to QEMU, and runs `source gdr.py` **once**. All tests in the
   suite reuse this single GDB connection, keeping convenience
   functions and pretty-printers registered across tests.
3. Each test calls `gdb_session.run("rtthread threads")` to execute a
   GDB command and capture output. ANSI escape sequences and PTY
   artifacts are stripped automatically.

This approach (borrowed from `pytest-embedded-jtag`'s `Gdb` class) is
preferred over spawning a fresh GDB batch process per test: it is faster
and avoids registration-state loss between tests.

### Target profiles

`GDR_QEMU_TARGET` selects the profile while keeping all GDR assertions shared:

| Target | QEMU startup | GDB symbols | Notes |
|--------|--------------|-------------|-------|
| `cortex-a9` | `qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` | `rtthread.elf` | Uses the ARM BSP's SD image. |
| `rv64` | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin` | `rtthread.elf` | M-Mode boot, no SD image, `set architecture riscv:rv64`. |

The ELF and firmware image are deliberately separate for RV64: QEMU's M-Mode
BSP boots the raw BIN, while GDB requires DWARF symbols from the ELF. The
shared suite also asserts the target pointer width, so the RV64 profile must
report `sizeof(void *) == 8`.

The RV64 matrix covers RT-Thread v4.0.4, v4.0.5, v4.1.0, and v4.1.1. The BSP
is `bsp/qemu-riscv-virt64` through v4.1.0 and is renamed to
`bsp/qemu-virt64-riscv` in v4.1.1; each path has a separate platform-specific
patch set.

Layout changes must update the corresponding assertion, keeping the
helper and the kernel struct in lockstep.
