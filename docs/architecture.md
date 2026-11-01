# Architecture

## Goals

Reduce the cognitive load of debugging complex RTOS-based embedded
firmware in GDB by:

- Folding noisy wrapper-type output (pretty-printers).
- Solving object navigation once (convenience functions).
- Aggregating multi-object state and diagnostics data (commands).

Non-goals: replacing GDB expressions, wrapping QEMU monitor commands, or
duplicating what GDB already displays well. Design rationale lives in
`decisions.md`, verification in `testing.md`, and known limits in `errata.md`.

## Layering

```text
                     gdr.py  (entry: arg parse, bootstrap, register)
                        |
        +---------------+----------------+
        |                                |
      core/  (RTOS-agnostic core)      selected RTOS adapter
         |                                |
  adapter.py / commands.py / api.py   layout.py / navigation.py
  version.py / gdb_bridge.py          adapter.py / diagnostics.py
  layout.py / heapmap.py / help.py    commands.py / views.py
```

### `core/` — RTOS-agnostic core

| Module | Responsibility |
| -------- | --------------- |
| `gdb_bridge.py` | GDB registration, identifier-only symbol lookup, memory/type access, ELF/DWARF macro-table walk, terminal probing, output and error guards. Inferior function evaluation is not used. |
| `constants.py` | Shared traversal limits, string/table defaults, and the API envelope `SCHEMA_VERSION`. |
| `formatting.py` | Pure optional/address/symbol/table formatting plus RTOS-neutral derived values (wrap-safe timer expiry, fill-byte watermark, waiter cells). No GDB dependency. |
| `layout.py` | Generic `StructLayout` / `StructField` / `ListHook` dataclasses and accessors (`read_field`, `iter_list`, `container_of`), plus layout-driven pretty-printer registration. Type matching uses `strip_typedefs().unqualified().tag` so typedef-spelled values resolve. |
| `version.py` | RTOS-neutral version parsing/decoding, `TargetVerdict`, and the `ProbeCandidate` registry behind `gdr probe`. |
| `adapter.py` | `RtosAdapter` protocol, presentation dataclasses (`Cell`, `ColumnSpec`, `ObjectTable`, `ObjectDetail`, `SystemSummary`, `HeapReport`, `CheckFinding`), and the active session. |
| `api.py` | Neutral verb layer (`tasks`/`objects`/`detail`/`backtrace`/`system`/`heap`/`snapshot`/`check`/`table`/`heap-dump`) over the same collectors the text renderers use, plus the JSON envelope and chunked evaluate transport. |
| `commands.py` | Shared collectors and guarded text renderers, the stop-time watch set, and convenience functions (`$gdr_task`, `$gdr_object`, `$gdr_json`). |
| `heapmap.py` | Heap-block layout to grid, owner palette, PNG and inline-terminal frames, including the dependency-free bitmap lettering for legends. Pure; no GDB import. |
| `unwind.py` | Bounded task unwind driver: seed frames, EHABI/CFI/frame-pointer selection, stack/PC/progress validation, per-frame method and stop reason. Pure; no GDB import. |
| `unwind_meta.py` | Bounded ELF sections, code symbols, zero-size symbol anchors and shared binary encodings. |
| `cpus.py` | Bounded Arm/RISC-V CPU ISA/ABI evidence from ELF attributes; contains no RTOS policy. |
| `stack_frame_parser.py` | Immutable saved-frame profiles with buffer spans, explicit slot aliases and synthetic/file-only/live validation tiers; only live profiles pass runtime gates. Also provides bounded RISC-V code evidence; adapter selectors own image/configuration proof. |
| `unwind_cfi.py` | Bounded DWARF CFI (`.debug_frame`/`.eh_frame`) tables and encoded pointers; rejects unsupported rules and unavailable pointer bases. |
| `unwind_ehabi.py` | Bounded ARM EHABI (`.ARM.exidx`/`.ARM.extab`) tables and virtual-register unwinding. |
| `help.py` | Structured help schema with terminal/Markdown renderers; command metadata stays structured so the same source feeds GDB now and docs later. |

### `rtthread/` — RT-Thread adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | **The only place that knows RT-Thread struct layouts.** `RtConfig`, symbol-presence probing, `build_layouts(config)`, version policy and probe evidence. |
| `navigation.py` | Registry/current-thread/tick symbols, type codes, timer traversal, halted system-heap snapshot. Returns raw `gdb.Value`. |
| `stackpc.py` | Last PC from the version-specific frame, retaining the Cortex-A9 path. Verified RV64 uses the selected saved-context profile. |
| `backtrace.py` / `backtrace_gdb.py` | Pure RT-Thread saved-context layouts and GDB-facing thread orchestration; only the compiled and closed-loop RV64 profile is enabled. |
| `stack_frame_profile.py` | ISA selection and compiled RV64 context-switch evidence; unverified profiles remain gated. |
| `adapter.py` | Intermediate models, value conversion, task/object tables, detail dispatch, system summaries. |
| `views.py` | Column contracts, typed detail-table navigation cells, heap views and verdicts. |
| `diagnostics.py` | Bounded raw-memory walks (IPC, heap), consistency diagnostics, and `rtt check`. |
| `commands.py` | The `rtthread` / `rtt` router, completion, and structured help tree. |

### `freertos/` — FreeRTOS adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | Config/DWARF probes, struct paths, version policy and probe evidence. |
| `navigation.py` | Scheduler-list traversal and six-channel object discovery (MPU pool, registry, active lists, symbols, waiter hosts, user addresses). |
| `stackpc.py` | Last PC projection from the selected saved-context profile. |
| `backtrace.py` / `backtrace_gdb.py` | Pure saved-context layouts (M0/M3/M33 SMP/MPU/RV32/RV64) and the GDB-facing seed/task orchestration for verified FreeRTOS ports. |
| `stack_frame_profile.py` | ISA/ABI, symbol, configuration and compiled RISC-V save-layout evidence for verified profile selection. |
| `objects.py` | Per-kind decode: software timers, event groups, stream/message buffers. |
| `heap.py` | Heap-1..5 snapshot, canary decode, free-list/linear walks with cross-check verdicts. |
| `adapter.py` | Task/object models, conversion, tables, summaries, `RtosAdapter` surface. |
| `views.py` | Column contracts, vertical detail builders, merged detail tables. |
| `diagnostics.py` | Bounded consistency checks and `frt check`. |
| `commands.py` | The `freertos` / `frt` router, completion, and structured help tree. |

## Runtime data flows

Initialization selects the adapter and builds its layout before registration:

```text
gdr init -> RTOS version policy -> config probes/layout -> adapter
         -> printers/functions/api registration
```

Machine-readable clients use the same collectors as the text commands:

```text
gdr api <verb> / $gdr_json -> collector -> active adapter
  -> ObjectTable/ObjectDetail/SystemSummary/HeapReport
  -> envelope JSON (inline, or chunked when it exceeds one evaluate result)
```

Aggregate commands use the adapter-owned presentation path:

```text
rtt/freertos command -> generic coordinator -> active adapter
  -> RTOS navigation/layout -> adapter intermediate model
  -> ObjectTable/ObjectDetail/SystemSummary
  -> shared formatting -> GDB output
```

Task stack backtraces resolve the task, decode its TCB/port context, parse
the loaded ELF's unwind sections and walk bounded frames:

```text
rtt/frt backtrace <task> / gdr api backtrace -> adapter
  -> saved/live seed registers + task stack bounds
  -> core.unwind_meta (ELF sections and symbols)
  -> core.unwind_cfi / core.unwind_ehabi (unwind metadata)
  -> core.unwind (metadata selection, optional frame pointer, bounds/depth proof)
  -> frames + method + complete/stop_reason
```

CFI register recovery removes unavailable values and carries their failure
reasons into subsequent steps; an undefined or unreadable register cannot
serve as a later CFA base or register-copy source. Known FDE ranges retain
CIE/rule errors. Parse failures with undecodable coverage prohibit
frame-pointer fallback unless an independent metadata rule covers the PC;
clean uncovered PCs retain the bounded fallback.

Native inspection stays separate:

```text
$gdr_task/$gdr_tasks/$gdr_object -> active adapter -> pure RTOS navigation
  -> raw gdb.Value or target-native pointer array
GDB p/bt/info -> registered printer -> active layout metadata -> GDB display
```

## Core mechanisms

### Unified routing through the active adapter

Every renderer resolves the target through `adapter.active()` and dispatches
on the `RtosAdapter` protocol, so `core/` never branches on an RTOS name. A
new RTOS only implements the protocol to obtain the command tree,
pretty-printers and convenience functions. `gdr reset` (and `gdr init
--force`) clears the session together with adapter caches and the
layout-derived printer lookup, so re-initializing cannot fold objects through
the previous adapter's layout.

### Typed presentation contract for machine clients

A row is built from `Cell(text, value, source)` objects, and
`ObjectTable.from_specs` derives `rows`, `value_rows` and `source_rows` from
the same cells, so a column cannot be typed into the wrong position. Each
adapter declares its columns once in `<rtos>/views.py` (`ColumnSpec` kinds,
alignment, shrink priority), and the capability claim derives from the same
declaration. Tables name their identity column (`id_key`, usually `Addr`) so
clients keep selection state across refreshes. Detail blocks keep `pairs` as
the complete flat view and add `sections` with per-row `ok`/`fail`/`skipped`
severity, so grouping adds severity without replacing terminal output.

### Extra derived data for debugging

List and detail output add derived state computed from the target's own
structures: symbolized addresses, `count@names` waiter summaries from live
suspend-list walks (never cached counters), capacity/policy columns, and
bounded corruption-guarded diagnostics. Tables adapt to the terminal width by
shrinking only adapter-marked `elastic` text columns; numeric, state, and
address cells are never truncated. Critical fields (waiters, `Addr`,
`ExpiresIn`, priorities, SMP placement) survive any adaptation; deep state
stays in singular details or native `gdb.Value` inspection. Every memory size
prints in bytes (`<n> B`); table headers stay plain names and byte columns
carry the unit in their documentation.

### Exception guard for kernel data collection

Any target read can raise `gdb.error` / `gdb.MemoryError`. Every command
dispatch and shared renderer is wrapped in `gdb_command_guard` (recoverable
reads warn and continue; anything else prints a one-line diagnostic, or the
full trace with `GDR_DEBUG`). Convenience functions use `gdb_function_guard`,
which converts errors into `gdb.GdbError`. Probe helpers degrade to safe
defaults on the shared `TARGET_ACCESS_ERRORS` set; the API envelope boundary
never raises, converting failures into coded errors (`target_unavailable`
for reads a client retries after the next stop, `internal` otherwise).

## Adapter capability comparison

Boundary details behind each row live in `errata.md`.

| Debugging dimension | RT-Thread (`rtt`) | FreeRTOS (`frt`) |
| --- | --- | --- |
| **Versions and architectures** | `3.1.0–3.1.5`, `4.0.0–4.1.1` (ARM Cortex-A9 32-bit, RISC-V 64-bit) | `10.3.0–11.3.x` (ARM Cortex-M3 / M33 SMP and MPU 32-bit, RISC-V 64-bit) |
| **Object discovery** | Kernel global object registry plus the active timer lists | Six-channel evidence merge (MPU pool, registry, active lists, symbols, waiter hosts, user addresses) with `Src` provenance and a `Queue_t` discriminator (`?` when inferred) |
| **Task basics** | TCB keeps `Entry`; `Stack`/`Used`/`HighWater` always derivable; `Error` and `RemainingTick`; SMP `CPU`/`Bind` | No retained `Entry`; `Stack`/`Used`/`HighWater` need `pxEndOfStack`; `Runtime`, `WakeTick`/`BlockedOn`, `Notify[n]`; SMP `CPU`/`Affinity` |
| **Last PC (`stackpc.py`)** | Slot keyed by version and pointer width; entry/call/code proof | Slot keyed by port evidence (build attributes and port symbols); code symbol plus Cortex-M Thumb-bit proof |
| **Task backtrace (`backtrace`)** | RV64 verified; A9 stays on `stackpc`, M0/M3/M33/RV32 report `unverified-port` | M0/M3/M33 SMP+MPU/RV32/RV64 verified on integer-only port configurations; EHABI preferred, DWARF CFI and validated frame-pointer fallback; SMP task on another core reports `smp-no-context` |
| **IPC and sync objects** | Semaphores, mutexes, events, mailboxes (with slot addresses), message queues, memory pools; unified `FIFO`/`PRIO` policy | Queue family (queues with FIFO item addresses and set membership, binary/counting semaphores, recursive mutexes), event groups with per-waiter conditions, stream/message/batch buffers |
| **Software timers** | Hard/soft and one-shot/periodic modes; registry vs active-list `Src` evidence | Current/overflow epoch handling, symbolic IDs, daemon command-queue decode |
| **Heap (`heap` / `heap dump`)** | `small_mem`, `memheap`, `slab`; per-thread owner colours with `MEMTRACE`; walk-vs-counter accounting | `heap_1..5` (heap_3 wraps malloc); no owner field so no per-task attribution; protector canary decode; free-list/linear cross-check |
| **Consistency checks (`check`)** | `system`, `heap`, `thread`, `timer`, six IPC kinds (name collisions, list integrity, block headers) | `system` (ready bitmaps, delay lists, unblock time), `heap`, `task`, queue family, `timer`, `eventgroup` |
