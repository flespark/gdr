# Architecture

## Goals

Reduce the cognitive load of debugging complex RTOS-based embedded
firmware in GDB by:

- Folding noisy wrapper-type output (pretty-printers).
- Solving object navigation once (convenience functions).
- Aggregating multi-object state and diagnostics data (commands).

Non-goals: replacing GDB expressions, wrapping QEMU monitor commands, or
duplicating what GDB already displays well. Design rationale lives in
[`decisions.md`](decisions.md), backtrace pipeline and build rules in
[`backtrace.md`](backtrace.md), verification in [`testing.md`](testing.md), and
known limits in [`boundary.md`](boundary.md).

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
| `elf.py` | Bounded ELF sections, `PT_GNU_EH_FRAME` resolution, code symbols, common binary encodings, and Arm/RISC-V CPU ISA/ABI attributes (`RegisterConvention`). |
| `insn_arm.py` | Bounded ARM/A32 instruction decoder with normalized mnemonic/operand evidence for instruction-leaf proofs and compiled port checks. |
| `insn_riscv.py` | Bounded RISC-V instruction decoder and ELF code-range primitives for port evidence checks. |
| `insn_thumb.py` | Bounded Arm Thumb/Thumb-2 instruction and stream decoder for port evidence checks. |
| `stack_frame_parser.py` | Immutable saved-frame profiles with buffer spans, explicit slot aliases, bounded slot readers, typed xPSR/CPSR decoding and synthetic/file-only/live validation tiers. |
| `backtrace/unwind.py` | Bounded task unwind driver: seed frames, live register seed dispatch, ELF metadata cache, EHABI/CFI/instruction-leaf/frame-pointer selection, stack-segment/PC/progress validation, evidence-gated transitions, per-frame method and stop reason. |
| `backtrace/unwind_segments.py` | RTOS-neutral stack-segment ownership, transition evidence and independent exception-depth/read budgets. |
| `backtrace/unwind_arm.py` | Shared eight-word Arm M-profile integer frame recovery, Armv6-M/Armv7-M exception policies, SCB/VTOR/vector-table readers, agnostic MSP/vector-capacity resolvers and handler-mode unwind setup. |
| `backtrace/unwind_riscv.py` | Shared RISC-V Machine-mode trap frame recovery, generic trap-block prologue/epilogue/store analysis, IRQ stack bounds resolution and handler-mode unwind setup. |
| `backtrace/unwind_leaf.py` | Architecture-neutral, bounded scalar instruction proofs for straight-line functions that preserve SP/LR and return through LR. ELF function types and instruction state define the scan; unknown code, calls, branches and stack-bank changes refuse the proof. |
| `backtrace/unwind_cfi.py` | Bounded DWARF CFI (`.debug_frame`/`.eh_frame`), `.eh_frame_hdr` binary search on demand with safe fallback to linear `.eh_frame` parsing, and encoded pointers; rejects unsupported rules and unavailable pointer bases. |
| `backtrace/unwind_expr.py` | Pure integer CFI expression evaluation for CFA, register-save addresses and direct register values, with independent bytecode, step, stack and memory-read budgets. |
| `backtrace/unwind_ehabi.py` | Bounded ARM EHABI (`.ARM.exidx`/`.ARM.extab`) tables and virtual-register unwinding. |
| `help.py` | Structured help schema with terminal/Markdown renderers; command metadata stays structured so the same source feeds GDB now and docs later. |

### `rtthread/` — RT-Thread adapter

| Module | Responsibility |
| -------- | --------------- |
| `layout.py` | **The only place that knows RT-Thread struct layouts.** `RtConfig`, symbol-presence probing, `build_layouts(config)`, version policy and probe evidence. |
| `navigation.py` | Registry/current-thread/tick symbols, type codes, timer traversal, halted system-heap snapshot. Returns raw `gdb.Value`. |
| `stackpc.py` | Last PC from the version-specific frame, retaining the Cortex-A9 path. Verified RV64 uses the selected saved-context profile. |
| `stack_frame_profile.py` | ISA selection and decoded Arm/RISC-V save/restore/initial-context evidence (register bitmaps, SP switch, LWP header) without fixed instruction-stream fingerprints; unverified profiles remain gated. |
| `backtrace/backtrace.py` / `backtrace/backtrace_gdb.py` | Pure RT-Thread saved-context layouts and GDB-facing orchestration for integer Arm M/A-profile and RV32/RV64 contexts. Arm M-profile handler contexts use shared segment/exception validation; proven scheduler transitions select owner-consistent seeds per stage. Verified configurations and extension limits live in [RT-Thread boundaries](boundary.md#rt-thread-backtrace-matrix). |
| `backtrace/arm_scheduler_switch.py` | Pure compiled Arm M-profile (`armv6-m`, `armv7-m`, `armv7e-m-fpu`, single-Secure-state `armv8m-integer`/`fpu`) scheduler transition profiles (`rt_hw_context_switch`, `PendSV_Handler`, `rt_hw_context_switch_to`) and stage classification. |
| `backtrace/riscv_scheduler_switch.py` | Pure compiled RISC-V scheduler/trap transition profiles and stage classification (`rt_hw_context_switch_to`, `rt_hw_context_switch`, `trap_entry`, `rt_hw_context_switch_exit`, `spurious_interrupt`; distinguishing `virt64` `stacked-epc` from `e310` `deferred-mepc`). |
| `backtrace/scheduler_seed.py` | Owner-consistent seeds from classified Arm M-profile and RISC-V scheduler/trap stages. |
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
| `stack_frame_profile.py` | ISA/ABI, symbol, configuration and compiled save-layout/trap-frame evidence for verified M-profile (`aapcs32`/`aapcs32-vfp`) and RISC-V (`10.3.x–11.3.1`) profile selection. |
| `backtrace/backtrace.py` / `backtrace/backtrace_gdb.py` | Pure saved-context layouts (Armv6-M, Armv7-M, Armv7E-M, Armv8-M Mainline SMP/MPU, RV32, RV64) and GDB-facing seed/task orchestration; basic and FPU handler-frame transitions use ISA-specific validation and verified port evidence. |
| `backtrace/arm_scheduler_switch.py` | Pure compiled Arm M-profile (`ARM_CM0`, `ARM_CM3`, `ARM_CM4F`, `ARM_CM33_NTZ`) transition profiles and instruction-relative stage classification; GDB-facing collection remains in `backtrace/backtrace_gdb.py`. |
| `backtrace/riscv_scheduler_switch.py` | Pure compiled RISC-V scheduler/trap transition profiles and CFG-partitioned stage classification (`xPortStartFirstTask`, trap handlers, `vTaskSwitchContext`) across `10.3.x–11.3.1` port variants. |
| `backtrace/scheduler_seed.py` | Owner-consistent seeds from classified Arm M-profile (basic/FPU) and RISC-V scheduler/trap stages. |
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

Task stack backtraces resolve the task or halted context, parse the loaded
ELF's unwind sections, and step bounded frames (details in
[`backtrace.md`](backtrace.md) and [`boundary.md`](boundary.md#task-backtraces)):

```text
rtt/frt backtrace <task> / gdr api backtrace -> active adapter (backtrace/backtrace_gdb.py)
  -> saved task profile (backtrace/backtrace.py + stack_frame_profile.py)
     or live/handler/scheduler seed (backtrace/scheduler_seed.py / unwind_arm.py / unwind_riscv.py)
  -> core.elf (ELF sections, sized symbols, CPU attributes, code bytes)
  -> core.backtrace (strict EHABI -> CFI -> leaf -> gap-only FP, segments, budgets)
  -> frames + method + complete/stop_reason + messages
```

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

Boundary details behind each row live in `boundary.md`.

| Debugging dimension | RT-Thread (`rtt`) | FreeRTOS (`frt`) |
| --- | --- | --- |
| **Versions and architectures** | `3.1.0–3.1.5`, `4.0.0–4.1.1` (Arm M/A-profile 32-bit, RISC-V 32/64-bit; verified version/port combinations in [boundaries](boundary.md#rt-thread)) | `10.3.0–11.3.x` (Armv6-M, Armv7-M, Armv8-M Mainline SMP and MPU 32-bit; RISC-V 32/64-bit) |
| **Object discovery** | Kernel global object registry plus the active timer lists | Six-channel evidence merge (MPU pool, registry, active lists, symbols, waiter hosts, user addresses) with `Src` provenance and a `Queue_t` discriminator (`?` when inferred) |
| **Task basics** | TCB keeps `Entry`; `Stack`/`Used`/`HighWater` always derivable; `Error` and `RemainingTick`; SMP `CPU`/`Bind` | No retained `Entry`; `Stack`/`Used`/`HighWater` need `pxEndOfStack`; `Runtime`, `WakeTick`/`BlockedOn`, `Notify[n]`; SMP `CPU`/`Affinity` |
| **Last PC (`stackpc.py`)** | Slot keyed by version and pointer width; entry/call/code proof | Slot keyed by port evidence (build attributes and port symbols); code symbol plus Arm M-profile Thumb-bit proof |
| **Task backtrace (`backtrace`)** | Compiled port evidence selects integer saved contexts; EHABI/CFI and instruction-proven leaves recover bounded saved/current chains. Unstarted roots require the retained entry and initial fill; configuration and IRQ acceptance are listed in [boundaries](boundary.md#rt-thread-backtrace-matrix). | Armv6-M, Armv7-M, Armv8-M Mainline SMP+MPU, RV32 and RV64 verified on integer-only port configurations; EHABI preferred, DWARF CFI and validated frame-pointer fallback; SMP task on another core reports `smp-no-context` |
| **IPC and sync objects** | Semaphores, mutexes, events, mailboxes (with slot addresses), message queues, memory pools; unified `FIFO`/`PRIO` policy | Queue family (queues with FIFO item addresses and set membership, binary/counting semaphores, recursive mutexes), event groups with per-waiter conditions, stream/message/batch buffers |
| **Software timers** | Hard/soft and one-shot/periodic modes; registry vs active-list `Src` evidence | Current/overflow epoch handling, symbolic IDs, daemon command-queue decode |
| **Heap (`heap` / `heap dump`)** | `small_mem`, `memheap`, `slab`; per-thread owner colours with `MEMTRACE`; walk-vs-counter accounting | `heap_1..5` (heap_3 wraps malloc); no owner field so no per-task attribution; protector canary decode; free-list/linear cross-check |
| **Consistency checks (`check`)** | `system`, `heap`, `thread`, `timer`, six IPC kinds (name collisions, list integrity, block headers) | `system` (ready bitmaps, delay lists, unblock time), `heap`, `task`, queue family, `timer`, `eventgroup` |
