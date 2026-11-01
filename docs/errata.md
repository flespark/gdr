# Errata

Known limits of the debugging model and the fixture evidence behind each
decoder branch. A constraint is a *current* limitation (what GDR cannot do
or what a lane cannot falsify), not a development rule — development rules
live in `AGENTS.md` and the per-lane READMEs.

## General

**Kernel debug info drives every probe.** Every adapter reads versions,
config features and struct shapes from DWARF and `-g3` macro tables, never
from version strings or a parsed `.config`.  A build stripped of debug info
(or a halt in a CU the macro table does not cover) degrades specific probes
to safe defaults with a warning, never a guess.

**SMP per-core state comes from shared memory, not per-core registers.**
QEMU models a second core as a separate GDB *inferior*, so per-core register
reads would need `target extended-remote`; every SMP assertion instead
derives from shared structure (`pxCurrentTCBs[]` for FreeRTOS, `rt_cpu_index`/
halted registers for RT-Thread).

## RT-Thread

**3.1.x has no QEMU RV64 BSP.** Upstream's RV64 BSP starts at 4.0.4
(`bsp/qemu-riscv-virt64`, renamed `bsp/qemu-virt64-riscv` in 4.1.1), so the
3.1.x range is verified on Cortex-A9 only; the rv64 lane covers 4.0.4 through
4.1.1.

**Fixture patch sets fix `RT_NAME_MAX`.** Cortex-A9 fixture patches set
`RT_NAME_MAX` to 16 to preserve the canonical `test_mutex` / `test_timer`
names every shared test asserts on (the RV64 BSPs already use 20).  The
object enum value for `Null` predates 3.1.3 and is asserted only on the
3.1.x tags whose layout is pinned.

**Heap-manager shape is probed, not asserted.** `small_mem` / `slab` /
`memheap` change the block-header structure entirely, so the heap walks are
keyed off the probed `heap_type`; missing counters render `N/A`, never a
fabricated 0.

## FreeRTOS

**Live coverage is 32-bit Cortex-M0/M3/M33 plus RV32/RV64 RISC-V.**
The 32-bit ARM lanes are `microbit` Cortex-M0, `mps2-an385` Cortex-M3,
`mps2-an521` dual-core Cortex-M33 and the Cortex-M33 static snapshot; the
RISC-V lanes are `qemu-virt-rv32` (rv32imac) and `qemu-virt-rv64`
(rv64imac); the latter covers 64-bit pointers and 64-bit ticks (the RISC-V port hardcodes
`TickType_t` to the architecture width; the lane's config also declares
`configTICK_TYPE_WIDTH_IN_BITS` 64 so the kernel's event-group control
bits sit in the top byte, matching the tick-width-derived masks).  Pointer
and field widths are always taken from
DWARF (`read_path` reads each union arm at its target type, and the reserved
MPU-pool handle is compared at the target pointer width), so no literal width
constant exists in the adapter.

**Version detection depends on `-g3` macro debug info and CU scope.**
`detect_target_version()` reads the `tskKERNEL_VERSION_*` macros via
identifier eval and `info macro`. Both consult the current compilation unit's
`.debug_macro` first; `info macro -a` plus the fixture-exported
`gdr_freertos_version_num` cover a halt in HAL / startup / application CUs.
A remaining miss degrades to a warning ("target FreeRTOS version is not
exported") and skips the mismatch check rather than guessing.

**Live fixtures are a variant matrix**, not a single configuration.
`ci/freertos/fixture/config/<variant>/` plus a shared `main.c` produce
`base` (the historical 10.3.1 combination), `full`,
`static-only`, `static-dynamic`, `trace-off`, `heap-1`/`2`/`3`/`5`,
`heap-protector` (≥V11), `heap-5-protector` (heap_5 + the protector, one
region), `registry-0`, `pend-callback` (`INCLUDE_xTimerPendFunctionCall`),
`streams` (write/read/delete actions on the stream/message/batching
buffers), `mpu` (single-core `configENABLE_MPU`, wrappers v2), `smp`
(dual-core 11.3.1) and `tick16` (`configUSE_16_BIT_TICKS 1`, a 2-byte
`TickType_t` on either ARM lane's kernel). Cache layout is
`<cache>/<target>/<version>/<variant>/freertos.elf`. Independent expected
capabilities live in `tests/support/freertos_fixture_profiles.py` and must not
be derived from `freertos/layout.py`.

The historical `base` combination remains: single-core / Cortex-M3 / heap_4
/ trace_facility=on / FreeRTOS 10.3.1 / `configENABLE_BACKWARD_COMPATIBILITY=1`
(member name `pvContainer`) / no `pxEndOfStack` / scalar `ucNotifyState` /
no runtime statistics / `configMAX_PRIORITIES=6` / stack grows down. Default
builds still use `pvContainer`; without `pxEndOfStack`, `Stack`/`Used`/`HighWater`
are unavailable because the saved SP does not retain stack capacity.

**Not covered by any current fixture (documented, unit-tested only):**

- `stack_grows_up`: **not supported**. GDR decodes FreeRTOS stacks as
  grow-down only (high water mark scans the untouched fill from the low end).
  The sole upstream `portSTACK_GROWTH +1` port is SDCC/Cygnal 8051, which has
  no GCC toolchain and no QEMU machine, so no grow-up target can exist; the
  dead field/branch was removed rather than kept as an untestable probe.

Live fixture evidence covers the MPU-pool channel (`mpu` variant), event-group control
bits at 16/64-bit tick widths (`tick16` / the RISC-V lane), the timer
pended-callback arm (`pend-callback`), stream-buffer ring wrap and the
deleted-buffer signature (`streams`), and `ListIntegrityBytes` on the static
snapshot.  Each variant's exact scope is documented in
`ci/freertos/README.md`; the snapshot variant carries the diagnostic negatives
a healthy kernel cannot produce.

**Static snapshot variant** (`ci/freertos/build.sh --variant snapshot`,
cell `mps2-an521/11.1.0`, sources in `ci/freertos/fixture/config/snapshot/`)
covers states a healthy kernel cannot produce
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

## Task backtraces

**Verified saved-context matrix.** FreeRTOS backtraces are verified on
Cortex-M0 (`microbit`), Cortex-M3 (`mps2-an385`), Cortex-M33 SMP and MPU
(`mps2-an521`), RV32 (`qemu-virt-rv32`) and RV64 (`qemu-virt-rv64`).
The Cortex-M33 fixtures disable FPU and TrustZone; other extended-context
configurations are not verified.
Armv8-M profile selection requires explicit FPU, TrustZone and MPU configuration
evidence (plus the wrapper version for MPU); absent required macros stop at
`port-configuration-unavailable`. Arm selection uses attributes, port symbols
and configuration rather than compiled save/restore instruction matching.
Backtrace JSON reports canonical ISA in `architecture` and ELF ABI in `abi`;
profile identity remains internal. RISC-V profiles require a bounded match of
the compiled integer save/restore and initial-stack code; this does not prove
vendor-private traps.
RT-Thread backtraces are verified on RV64; its M0/M3/M33/RV32 context layouts
are implemented and unit-tested but report `unverified-port` until a QEMU BSP
fixture exists. Cortex-A9 remains on the existing `stackpc` path and is not a
first-version backtrace target.

**EHABI entries require a matching ELF function symbol.** Without
`-funwind-tables` the ARM fixtures expose only `EXIDX_CANTUNWIND`. With the
tables enabled, GDR accepts an exidx entry only when its start equals the
enclosing sized `STT_FUNC` symbol; a previous function's entry cannot cover a
gap where the compiler emitted no row. If the symbol table is stripped, CFI or
the validated frame-pointer chain is used instead.

**CFI supports a bounded integer-rule subset.** `DW_CFA_offset_extended`,
CIE return columns and signal-CIE exact PCs are supported; an undefined
return column stops at `cfi-return-address-undefined` with `complete=false`.
The three expression rules stop at `cfi-expression-unsupported`. Pointer
encodings requiring unavailable text/function bases or indirect reads are
rejected. The standalone `.eh_frame` verification scope is recorded in
[testing](testing.md#standalone-eh_frame-unwinding).

**FreeRTOS does not record a stack end in every build.** Without
`configRECORD_STACK_HIGH_ADDRESS` or another retained stack-end field, the
report has no frames and stops at `stack-bounds-unavailable`. RT-Thread
supplies `stack_addr`/`stack_size`; unavailable bounds also prevent a walk.

**Unstarted tasks require an explicit proof.** RT-Thread requires the saved
PC to equal the TCB entry and the integer context to carry the port's initial
fill. FreeRTOS retains no independent task-entry field, so its adapter never
reports `source=unstarted` from initial registers, xPSR or an exit sentinel
alone. The pure decoder requires an independently supplied entry address.

**RISC-V first-task return state is not a normal call chain.** The FreeRTOS
port starts its first task with RA equal to the entry PC, not a call's resume
address. Without independent task-entry evidence, ordinary `PC - 1` lookup
can select the preceding function; the RV32/RV64 fixtures stop at
`cfi-stack-overflow` with `complete=false`. An entry-looking RA must not
be promoted to an exact PC or a proven task root. Repeated `(PC, SP)` pairs
still stop at `loop`. Other tasks can reach the recorded `xTaskReturnAddress`
value, including zero, and finish at `task-exit`.

**Hard-interrupt and nested-exception cross-stack unwinding is not supported.**
Recovering a task seed from a port's saved hardware exception frame does not
provide handler-to-task unwinding. The driver uses a single stack span; it
does not decode exception-return transitions, switch task/interrupt stack
bounds, or join nested handler frames to the interrupted task. Saved-context
lanes exercise blocked/yielding tasks. FPU/TrustZone extended frames and other
unverified context variants are outside the verified matrix.

**SMP tasks on another core have no saved context.** A task currently running
on a core other than the selected one reports `smp-no-context`; GDR never
reads that core's live registers through another core's context.

**No stack scanning and no inferior calls.** The frame-pointer fallback is
used only when EHABI/CFI has no row and CFI coverage is not obscured by a
parse error; it validates alignment, stack bounds,
progress and that the return PC is executable. GDR never scans a stack for
plausible return addresses, calls an inferior function, or writes target
memory.

**No `Entry` column.** The FreeRTOS TCB (`tskTaskControlBlock`) does not store
the task entry function pointer after task creation; it exists only transiently
on the initial stack frame and is overwritten on first context switch. This is
a fundamental difference from RT-Thread's `rt_thread.entry`, which persists in
the TCB. The `frt help` output documents this limitation.

**High-water mark semantics.** `HighWater` matches RT-Thread: historical maximum
stack bytes used, derived as `max(Used, recorded usable span - untouched fill)`.
FreeRTOS's native API instead returns remaining words; GDR converts and subtracts
at the model boundary, then includes the current saved-SP footprint because
reserved context slots may still contain `0xa5`. `pxEndOfStack` is inclusive of one `StackType_t`;
`Stack` and `Used` use that same upper bound, excluding alignment-discarded
allocation padding. Missing bounds, partial reads, or ambiguous unfilled/exhausted
stacks yield no value, never a fabricated total. Initial context reservations
count before scheduler startup, ensuring `Used <= HighWater <= Stack` whenever
all are available. A halted snapshot cannot recover historical reservations
that left no write trace or distinguish a write equal to the fill byte.
For MPU wrappers v2, `pxTopOfStack` addresses the TCB context array, not the
task stack: `Used` uses the decoded task SP, and fill scanning uses only the
recorded task span. An unreadable context can leave `Used` unavailable while
the retained span and fill still establish `HighWater`.

**Queue `type` field is gated by `trace_facility`.** The `ucQueueType` member
only exists under `configUSE_TRACE_FACILITY == 1`. Layout summary fields that
reference config-conditional struct members must be gated by the corresponding
`FreeRtosConfig` flag; unconditional inclusion produces `N/A` on builds where
the member is absent.

**Heap blocks carry no owner field, so per-task heap usage is not
attributable.** A `BlockLink_t` is exactly `{pxNextFreeBlock, xBlockSize}`
(heap_4.c) with no owner member; `frt heap` therefore never offers a thread-occupancy
breakdown, and `frt help` documents this instead of faking a column. `frt heap`
renders the `Algorithm/TotalSize/FreeSize/MinEver/Allocated/Freed/Protector/Blocks/
Holes` properties, using `N/A` for unavailable facts. `Allocated`/`Freed` are
cumulative successful operation counts, not bytes or live block counts.
`Check[HeapCrossCheck]` appears only for disagreements or incomplete walks;
`frt check heap` includes successful verdicts.

**heap_5 without `configENABLE_HEAP_PROTECTOR` has no linear-walk bounds.**
The kernel exports no region bases without the protector (`pucHeapLowAddress`/
`pucHeapHighAddress` only exist then, heap_5.c), the intermediate region-markers
can be unlinked by forward coalescing, and the region table is a function-local
array — so `frt heap` skips the linear walk, renders `Blocks` from the free-list
walk only and reports `Check[HeapCrossCheck]: skipped: heap_5 region bases unknown`.
With the protector a single-region heap can be walked from its base to `pxEnd`;
the `heap-5-protector` variant (heap_5 + `configENABLE_HEAP_PROTECTOR`, one
region) is the live fixture for that branch: the linear walk runs, the cross-check
passes and the region extremes become the true `TotalSize` (a multi-region
extent would span gaps, so the total is only set when the walk reaches `pxEnd`
-- the single-region witness).

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
resolved the walk is skipped (`Holes` is `N/A` and `Check[HeapCrossCheck]` explains why) rather
than truncated from the head. Live evidence on `mps2-an385/10.3.1/base`: the
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

**Live heap coverage includes one 64-bit lane.** The `qemu-virt-rv64` lane has
`sizeof(size_t) == 8`, so the 64-bit allocation-mask arithmetic
(`_allocated_bitmask` with the size_t MSB) and the `Heap used + Heap free ==
Heap total` arithmetic are live-verified there; `_pointer_bits` still falls
back to 32 only when no type information exists at all. The cross-check
`mismatch` and walk `corrupt` verdicts have live evidence from the static
snapshot variant's crafted cells (free-list skips a linear-free block; a
free-list member carrying the size_t MSB), since a healthy kernel cannot
produce a corrupt heap.

**Object discovery is a six-channel provenance model, not a registry walk.**
FreeRTOS keeps no global object registry for most kinds (only the optional
`xQueueRegistry` and the MPU wrappers v2 pool), so GDR discovers objects from
six channels, each of which attaches its origin to the object:

| channel | source | covers |
| --- | --- | --- |
| MPU pool | `xKernelObjectPool` (file-static, wrappers v2 only) | all kinds |
| registry | `xQueueRegistry`, whole array scanned (holes included) | queue/semaphore/mutex |
| active | `pxCurrentTimerList` / `pxOverflowTimerList` | timers (active only) |
| symbol | one `info variables` scan per session, cached; `Static*_t` buffers use the symbol address, `*Handle_t` uses the pointer value (translated through the kernel object pool on MPU builds, where the handle is an opaque slot index) | all kinds |
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

Automatic views show only named failures and incomplete reads/walks, retaining
`fail:`/`skipped:` prefixes so merged diagnostic columns preserve severity.
`frt check [scope] [name]` is the explicit exhaustive surface (passes and
structural skips included), with object addresses and observed pointer ranges,
fields or block locations. Checks never drive the target; an interrupted kernel
update can be transient. `frt system` uses `SystemSummary.extra_pairs` without
introducing RTOS semantics into the core. NextUnblockTime is skipped before
scheduler startup and while suspended; an earlier cached deadline is legitimate
after event unblocking, while a later cache reports the delayed item and wake tick.
Heap mismatches name differing free blocks and `xFreeBytesRemaining`; corrupt
walks retain their exact stop reason. No healthy-view aggregate counts claim that
an unperformed check passed.

A singular detail command refuses a kind it can prove wrong: resolving a name
or address stamps the *requested* kind on whatever it found, so a wrong
kind must be refused before rendering. The resolved object is refined
through the `Queue_t`
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

The `mpu-pool` channel is live on the `mpu` variant (see the "Closed by the
fixture matrix" list); the empty/reserved-slot skip, kind mapping and
QUEUE-slot `inferred_kind` semantics were unit-tested before any bootable
fixture existed.  On an MPU build the application-visible handle is an opaque
pool index (wrappers v2 `CONVERT_TO_EXTERNAL_INDEX` = slot + 1), never a
pointer, so the symbol and user channels translate such a value through
`xKernelObjectPool[value - 1].xInternalObjectHandle` (and skip a value that
is neither a valid index nor a pointer inside a mapped section) instead of
reading `0x3` as an address. The `waiter` channel is pinned by a deliberate
fixture object:
`gdr_waiter_only_eg_task` (ci/freertos/fixture/main.c) blocks forever on an
event group whose handle lives only on its own stack, so that group is
discoverable *only* through the waiter channel -- the live no-ghost assertion
now permits exactly one host (that anonymous event group) that no earlier
channel finds, and would fail if the container_of probe fabricated objects out
of plausible neighbouring memory beyond it.
