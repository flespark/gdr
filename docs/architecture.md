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

```
                     gdr.py  (entry: arg parse, bootstrap, register)
                        |
        +---------------+----------------+
        |                                |
      gdr/  (RTOS-agnostic core)      rtthread/  (RT-Thread v4.0.x adapter)
        |                                |
   gdb_bridge.py                   layout.py
   layout.py                       adapter.py
   printers.py                     commands.py
   kernel.py
   abstractions.py
```

### `gdr/` — core

| Module | Responsibility |
|--------|---------------|
| `gdb_bridge.py` | Wraps the GDB Python API: command/function registration, table printing, safe eval, error guards. Keeps `gdb.*` calls in one place so the rest of the code is testable. |
| `layout.py` | Generic `StructLayout` / `StructField` / `ListHook` dataclasses and accessors (`read_field`, `iter_list`, `container_of`). Knows nothing about RT-Thread. |
| `printers.py` | Pretty-printer registration framework. Wrapper-type printers fold locks/atomics/threads into one-line summaries. |
| `kernel.py` | Object navigation primitives: `find_thread`, `iter_threads`, `find_object`. Takes a `layouts` dict, never hardcodes field names. |
| `abstractions.py` | Minimal ABCs (`Thread`, `Semaphore`, `Mutex`, `Timer`, `Queue`) with `to_dict()`. No unimplemented abstract methods. |

### `rtthread/` — adapter

| Module | Responsibility |
|--------|---------------|
| `layout.py` | **The only place that knows RT-Thread struct layouts.** Defines `RtConfig`, `detect_config()` (symbol-presence probing), and `build_layouts(config) -> dict[str, StructLayout]`. Handles config-conditional fields (SMP, heap manager, IPC toggles) via factory branches, not version-branched files. |
| `adapter.py` | Value→dataclass converters (`value_to_thread`, `value_to_semaphore`, …) and `gdb.Function` subclasses (`$gdr_thread`, `$gdr_threads`, `$gdr_object`). The `_value_to_str()` helper handles GDB string literals whose `type.code` is `TYPE_CODE_ARRAY`, not `TYPE_CODE_STRING`. |
| `commands.py` | The 5 aggregate commands. Argument parsing + table output only; no struct knowledge. |

## Key decisions

### No RTOS / version auto-detection

Previous versions attempted to detect the RTOS and parse its version string
from symbols, then match struct patterns. This was fragile (failed on
attach, failed across remote configs) and duplicated logic. Users now
specify the RTOS and exact version, for example
`--rtos rtthread --version 4.0.5`. The RT-Thread adapter validates the
supported 4.x.x range, while layout differences are still handled by probing
target symbols and DWARF rather than branching on every patch version.

### Config features are probed, not specified

RT-Thread kernels vary by *configuration* far more than by version:
`RT_USING_SMP` adds `oncpu` to `rt_thread`; the heap manager
(`small_mem` / `slab` / `memheap`) changes the heap data structures
entirely; IPC components (`RT_USING_MUTEX`, etc.) may be absent.
Probing these by symbol presence (`rt_cpu_index`, `rt_smem_init`,
`rt_mutex_take`, ...) is reliable and cheap, and spares users from
reciting their `.config`. Probing falls back to safe defaults with a
warning when a symbol is ambiguous.

### Dataclass layout, not YAML schema

Considered an external YAML schema + loader. Rejected because:

- Structs vary by **config**, not version. YAML would need conditional
  fields / overlays, turning the "lightweight loader" into a mini
  interpreter — a new failure surface.
- Version-to-version struct deltas are small; per-version YAML files
  would be 99% duplicate.
- Python dataclasses handle config-conditional fields naturally via
  factory functions (`build_thread_layout(config)`) with no extra
  syntax or parser.

If a second RTOS (e.g. FreeRTOS) is added later, it gets its own
`freertos/layout.py` module; the core `gdr/layout.py` stays generic.

### Coupling is explicit and localised

All layout-sensitive knowledge lives in `rtthread/layout.py`. When an
RT-Thread struct changes, that single file — plus its QEMU smoke test
assertion — is the review surface. This mirrors the Asterinas
`constants.py` + `COUPLED` annotation discipline.

### Commands only aggregate

Per the Asterinas experience: commands should do what GDB expressions
cannot — iterate collections, tabulate, build trees. Single-object
field inspection is left to `$gdr_thread(name)` + `p (*.thr).field`.
This keeps the command set small and avoids commands silently breaking
when a field is renamed (the function returns the raw `gdb.Value`).

## Closed-loop verification

GDB helpers degrade silently: the script runs but output is wrong. To
guard against this, QEMU smoke tests boot an RT-Thread v4.0.x firmware
that creates known threads/semaphores/mutexes/timers, and assert:

- pretty-printers registered and fold correctly,
- convenience functions return non-null `gdb.Value` with expected fields,
- aggregate commands list the expected objects.

### Test infrastructure

Tests use a **persistent GDB session** driven by `pexpect`:

1. A session-scoped `QemuSession` starts QEMU with `-gdb tcp::1234`
   (free-running, no `-S`) and waits for kernel objects to be created.
2. A session-scoped `GdbSession` spawns one GDB process via `pexpect`,
   connects to QEMU, and runs `source gdr.py` **once**. All tests in the
   suite reuse this single GDB connection, keeping convenience
   functions and pretty-printers registered across tests.
3. Each test calls `gdb_session.run("rtthread threads")` to execute a
   GDB command and capture output. ANSI escape sequences and PTY
   artifacts are stripped automatically.

This approach (borrowed from `pytest-embedded-jtag`'s `Gdb` class) is
preferred over spawning a fresh GDB batch process per test: it is
faster (~12s for 22 tests vs minutes) and avoids registration-state
loss between tests.

Layout changes must update the corresponding assertion, keeping the
helper and the kernel struct in lockstep.
