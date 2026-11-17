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

**Live coverage is 32-bit Armv6-M/Armv7-M/Armv8-M Mainline plus RV32/RV64 RISC-V.**
The 32-bit Arm lanes are `microbit` (Armv6-M, Cortex-M0), `mps2-an385`
(Armv7-M, Cortex-M3), `mps2-an521` (Armv8-M Mainline, dual-core Cortex-M33)
and the Armv8-M Mainline static snapshot; the
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
`ci/fixture/freertos/profiles/config/<variant>/` plus a shared `main.c` produce
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

The historical `base` combination remains: single-core / Armv7-M (`mps2-an385`) / heap_4
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
`ci/fixture/freertos/README.md`; the snapshot variant carries the diagnostic negatives
a healthy kernel cannot produce.

**Static snapshot variant** (`ci/fixture/freertos/build.sh --variant snapshot`,
cell `mps2-an521/11.1.0`, sources in `ci/fixture/freertos/profiles/config/snapshot/`)
covers states a healthy kernel cannot produce
(corrupt lists, SMP `xTaskRunState` of `0`/`1`/`-1`/`-2` without a live
dual-core port, a timer on the overflow list, and corrupt-heap cells). Snapshot
data is non-zero-initialized into `.data`; file-only GDB synthesises zeros for
`.bss`, so a BSS-resident structure would read back as a successful decode of
empty lists (see `ci/fixture/freertos/README.md`). Zero-valued standalone globals are
forced into `.data` with `__attribute__((section(".data")))` for the same
reason. The heap *mismatch* cell (free-list vs linear vs counter disagree) and
the *allocated-bit* cell (free-list member with the size_t MSB) cannot share
one heap symbol set — a corrupt walk swallows the mismatch verdict — so the
allocated-bit cell lives in a second, heap-only ELF (`snapshot_heap.c`).

## Task backtraces

**Verified saved-context matrix.** FreeRTOS backtraces are verified on
Armv6-M (`microbit`), Armv7-M (`mps2-an385`), Armv7E-M (`mps2-an386`),
Armv8-M Mainline single-core (`mps2-an505`) and SMP/MPU (`mps2-an521`), RV32
(`qemu-virt-rv32`) and RV64 (`qemu-virt-rv64`).
The Armv8-M Mainline fixtures disable FPU and TrustZone; other extended-context
configurations are not verified.
Armv8-M Mainline profile selection requires explicit FPU, TrustZone and MPU configuration
evidence (plus the wrapper version for MPU); absent required macros stop at
`port-configuration-unavailable`. Arm selection uses attributes, port symbols
and configuration rather than compiled save/restore instruction matching.
Backtrace JSON reports canonical ISA in `architecture` and ELF ABI in `abi`;
profile identity remains internal. FreeRTOS ARM_CM4F basic and FPU saved-context
layouts are selected per task by the exact saved EXC_RETURN; the `backtrace`
orchestration dispatches through the same slot proof, and the AN386/11.3.1/fpu
lane verifies the pair live (204-byte FPU image, `0x68`-byte hardware frame).
Live VFP-ABI images without the CM4F port evidence stop at
`unsupported-extension-context` during selection. RISC-V profiles require a bounded match of
the compiled integer save/restore and initial-stack code; this does not prove
vendor-private traps.
RT-Thread integer backtraces are verified on RV32 (`rv32/4.0.5`), RV64, Armv6-M
(`microbit/4.0.5`), Armv7-M (`mps2-an385/4.0.5`) and Armv7-A
(`cortex-a9/4.0.5`). The Armv7-A profile covers the compiled SMP/signal/LWP-header
kernel context; user-mode seeds and FPU contexts are refused, and frame-pointer
fallback has no Armv7-A acceptance. Armv8-M Mainline (`mps2-an505/4.0.5`) covers
the 80-byte single-Secure-state integer frame: saved `tz=0`, `EXC_RETURN=0xFFFFFFFD`,
`PSPLIM=0` and privileged CONTROL (0 before first return, 2 while using PSP).
The current seed requires readable CONTROL/PSPLIM and a zero TrustZone context.
Non-secure/cross-security returns, nonzero stack limits and unprivileged
contexts remain refused; dual-world TrustZone switching stays unsupported.
The `mps2-an505/4.0.5/fpu` lane adds compiled FPU save/restore evidence
(`tst`/`it`/`vstmdb`/`vldmia` around the core block) and verifies the live
216-byte FPU image alongside the basic pair; other VFP-ABI images stop at
`unrecognizable-stack-frame-layout` during selection. The `mps2-an386/4.0.5/fpu`
lane adds compiled M4F save/restore evidence (`tst`/`it`/`vstmdb` around the
core block, per-thread flag slot, FPCA fixup) and verifies the live 204-byte
FPU image alongside the 68-byte basic pair; other VFP-ABI images stop at
`unrecognizable-stack-frame-layout` during selection. Armv8-M Baseline images
are identified by `Tag_CPU_arch` but select no profile (`unverified-port`):
no QEMU machine or CPU model covers Cortex-M23, so no live lane can exist
and handler/task support stays refused by construction.
RV32 and RV64 cover the upstream common integer ports and Machine-mode trap
cross-stack unwinding; vendor-private traps remain refused
(`unsupported-exception-model` / `exception-model-unverified`).

**EHABI entries require a matching ELF function symbol.** Without
`-funwind-tables` the ARM fixtures expose only `EXIDX_CANTUNWIND`. With the
tables enabled, GDR accepts an exidx entry only when its start equals the
enclosing sized `STT_FUNC` symbol; a previous function's entry cannot cover a
gap where the compiler emitted no row. If the symbol table is stripped, CFI or
the validated frame-pointer chain is used instead.

**CFI supports a bounded integer-rule subset.** `DW_CFA_offset_extended`,
CIE return columns and signal-CIE exact PCs are supported; an undefined
return column stops at `cfi-return-address-undefined` with `complete=false`.
The three expression rules use a bounded integer evaluator, not the full
DWARF location language. Register-location descriptions (`DW_OP_reg*`),
CFI-forbidden operations and unsupported typed/composite operations are rejected;
`DW_OP_stack_value` is accepted only as a terminal direct-value operation.
Pointer encodings (`DW_EH_PE_textrel`, `DW_EH_PE_datarel`, `DW_EH_PE_funcrel`,
`DW_EH_PE_indirect`) require explicit text/data/function bases or an indirect
memory reader via `EncodedPointerContext` and are verified on isolated RV64 ELF
images; `.eh_frame_hdr` index tables (`PT_GNU_EH_FRAME`) binary-search FDEs on
demand and degrade to `.eh_frame` parsing on index errors without masking
`.eh_frame` corruption. The standalone `.eh_frame` scope is recorded in
[testing](testing.md#isolated-eh_frame-variant).

**FreeRTOS does not record a stack end in every build.** Without
`configRECORD_STACK_HIGH_ADDRESS` or another retained stack-end field, the
report has no frames and stops at `stack-bounds-unavailable`. RT-Thread
supplies `stack_addr`/`stack_size`; unavailable bounds also prevent a walk.

**Unstarted tasks require an explicit proof.** RT-Thread requires the saved
PC to equal the TCB entry and the integer context to carry the port's initial
fill. FreeRTOS retains no independent task-entry field, so its adapter never
reports `source=unstarted` from initial registers, xPSR or an exit sentinel
alone. The pure decoder requires an independently supplied entry address;
the verified first-SVC path supplies the exact sized ELF function entry as
part of its separate startup proof.

**RISC-V first-task return state is not a normal call chain.** The FreeRTOS
port starts its first task with RA equal to the entry PC, not a call's resume
address. Without independent task-entry evidence, ordinary `PC - 1` lookup
can select the preceding function; the RV32/RV64 fixtures stop at
`cfi-stack-overflow` with `complete=false`. An entry-looking RA must not
be promoted to an exact PC or a proven task root. Repeated `(PC, SP)` pairs
still stop at `loop`. Other tasks can reach the recorded `xTaskReturnAddress`
value, including zero, and finish at `task-exit`.

**Hardware exception cross-stack unwinding is verified on Armv6-M/Armv7-M/Armv7E-M/single-Secure-state Armv8-M Mainline fixtures.**
FreeRTOS `microbit/11.1.0/full` (Armv6-M, `ARM_CM0`),
`mps2-an385/10.3.1/full` (Armv7-M, `ARM_CM3`), `mps2-an386/11.3.1/fpu`
(Armv7E-M, `ARM_CM4F`), and `mps2-an505/11.3.1/full` (single-core Armv8-M Mainline,
`ARM_CM33_NTZ`) plus the corresponding RT-Thread `4.0.5` M-profile lanes verify
external IRQs, SysTick, nested handlers, ordinary handler calls, and four-byte alignment
padding with linker-owned MSP bounds. `core/unwind_arm.py` shares the basic
eight-word frame layout while keeping ISA exception policies explicit;
Armv6-M has a six-bit IPSR and mandatory 8-byte entry alignment, while Armv7-M
has a nine-bit IPSR and CCR.STKALIGN-controlled alignment. Both require readable
xPSR/MSP/PSP, running-scheduler ownership and a proven MSP span resolved from
explicit `__gdr_irq_stack_low`/`__gdr_irq_stack_top` overrides, standard
CMSIS/toolchain symbols (`__StackLimit`..`__StackTop`, `_sstack`..`_estack`),
dedicated ELF stack sections (`.irq_stack`, `.isr_stack`, `.stack`), or the
vector-table initial MSP paired with a non-overlapping halted MSP. Armv7-M
requires ICTR capacity. Armv6-M requires Cortex-M0 CPUID (`0x410CC200`), a
fixed-zero vector table (`g_pfnVectors` or `__Vectors` at `0x0`), and external
IRQ capacity (1..32) resolved from explicit `__gdr_vector_table_end` and
`__gdr_armv6m_irq_count` overrides, `__Vectors_End`, `.isr_vector` section size,
or the first executable symbol after `0x0`; missing or
conflicting evidence fails closed. Cortex-M0+ CPUs, relocated vector tables and
unproven Armv6-M vector configurations have no live acceptance. Capacity upper bound 47 does not
establish a live handler for every IRQ; only exception numbers 24/25 have
fixture coverage. NMI and DebugMonitor stop as
`exception-number-unverified`. SysTick is admitted as a proven periodic
interrupt on both FreeRTOS and RT-Thread M-profile lanes (same ownership, IRQ
stack and vector-entry proof as external IRQs, including stops inside
`xPortSysTickHandler` / `xTaskIncrementTick` / `rt_tick_increase`; fault and
scheduler frames encountered mid-tick still refuse).
Synchronous faults cross the stack with `stop_reason=synchronous-fault`
only with precise CFSR/HFSR evidence (UsageFault bits, `IACCVIOL`/`DACCVIOL`,
`PRECISERR`+`BFARVALID` or `IBUSERR`, escalated HardFault with `FORCED` plus
one of those causes), including stops inside C fault handlers and
RT-Thread's `rt_hw_hard_fault_exception` / `rt_exception_hook` after the
`HardFault_Handler` assembly trampoline pushes its software context block onto
PSP; imprecise, stacking/lazy-stacking, vector-fetch and
debug-event faults stop as `fault-frame-imprecise` without presenting a task
chain. Single-Secure-state Armv8-M Mainline admits synchronous fault frames,
SysTick and external IRQs on both RT-Thread (`mps2-an505/4.0.5`) and single-core
FreeRTOS (`mps2-an505/11.3.1`); FreeRTOS `mps2-an521` SMP/MPU handlers,
TrustZone dual-world state (SecureFault, cross-security returns, nonzero SPLIM)
and SMP handler contexts stay `unsupported`. On QEMU AN505 an MPU-blocked data load
surfaces as a precise bus fault rather than the specified MemManage fault, so
DACCVIOL live proof stays on the Armv7-M/Armv7E-M lanes; IACCVIOL has
synthetic unit coverage only, since branching into an execute-never region
does not raise the specified direct fault on the QEMU MPS2 model and a fixture
must never contain a path that can wedge the shared halted session. Live fault
evidence covers the Armv7-M/Armv7E-M and single-core Armv8-M Mainline lanes of
both kernels (UsageFault escalation, precise bus, escalated or direct MemManage) and
the Armv6-M `fault-frame-imprecise` refusal on FreeRTOS `microbit/11.1.0/fault`;
there is no RT-Thread Armv6-M fault lane. Armv7-M/Armv7E-M/Armv8-M Mainline non-secure FPU extended frames
(`0xFFFFFFE1/E9/ED`) decode the `0x68`-byte frame when FPCCR is readable,
including lazy `LSPACT=1` (only integer registers are consumed), and stop at
`exception-fp-state-unavailable` otherwise; Armv6-M has no FPU and still
refuses extended encodings. Other saved-context lanes do not establish
handler-mode support; unproven ports and unmatched scheduler transitions stay refused.

**RISC-V M-mode trap cross-stack unwinding requires compiled trap-entry proof.**
FreeRTOS (`qemu-virt-rv32` and `qemu-virt-rv64`) and RT-Thread (`rv32` E310 and
`rv64` virt64) recover the interrupted task chain across the ISR stack from
verified direct-mode `mtvec` dispatch (`mstatus.MPP == M`, IRQ stack bounds from
kernel symbols or ELF sections, and exact raw-entry `mepc` or stacked/deferred
return address); nested deferred-`mepc` traps (`rt_interrupt_nest > 1` on E310)
stop at `nested-trap-epc-unproven`, active switch windows (`vTaskSwitchContext`,
`rt_thread_switch_interrupt_flag != 0`, and trap save/restore epilogues) fail
closed (`scheduler-transition-unproven`), synchronous `ecall` stops at
`exception-task-context-conflict`, and vendor-private traps remain unsupported.

**Scheduler backtraces require verified compiled M-profile port profiles.**
FreeRTOS verifies `ARM_CM0` (`freertos/armv6-m/arm-cm0-pendsv-v1`), `ARM_CM3`
(`freertos/armv7-m/arm-cm3-pendsv-v1`), `ARM_CM4F` (`freertos/armv7e-m/arm-cm4f-pendsv-v1`),
and non-secure single-core `ARM_CM33_NTZ` (`freertos/armv8-m.mainline/arm-cm33-ntz-pendsv-v1`)
across PendSV, `vTaskSwitchContext`, and first SVC startup stages; RT-Thread verifies
`armv6-m`, `armv7-m`, `armv7e-m-fpu`, and single-Secure-state `armv8m-integer`/`fpu`
across first-thread startup, PendSV save/restore stages, and `switch-return`.
Every other PC inside these functions stops as `scheduler-stage-ambiguous:*`.
Live stage-by-stage evidence covers all four M-profile ISAs on both RTOSes
([FreeRTOS/RT-Thread scheduler fixtures](testing.md#rt-thread-integration-testing-testsintegrationrtthread)).
On FreeRTOS, first SVC returns `source=unstarted` and `task-entry` only with
initial registers, exit sentinel and exact sized ELF function-entry evidence;
its software-prebuilt frame requires ALIGN=0, no padding and a 4-byte-aligned
base (live path still requires readable CCR), before SVC executes there is no
running-task seed, and PendSV selection of a never-started task remains refused.
On RT-Thread, `first_thread_start` (`from_ptr == 0`) decodes `source=unstarted`
for the incoming thread only after its saved frame is restored. Unmatched ports,
TrustZone dual-world states, and SMP scheduler transitions remain refused.

**SMP tasks on another core have no saved context.** A task currently running
on a core other than the selected one reports `smp-no-context`; GDR never
reads that core's live registers through another core's context.

**No stack scanning and no inferior calls.** The frame-pointer fallback is
used only when EHABI/CFI has no row and CFI coverage is not obscured by a
parse error; it validates alignment, stack bounds,
progress and that the return PC is executable. GDR never scans a stack for
plausible return addresses, calls an inferior function, or writes target
memory.

**Instruction leaf recovery uses a bounded scalar subset.** A clean metadata
gap admits a `leaf` step only for a typed ELF function with a proven
straight-line SP/LR-preserving register return. Unknown instructions, branches,
calls, stack-bank changes, untyped entry labels and unavailable LR stop that
path; failed or undecodable metadata cannot enable it. Clobbered registers are
not propagated into the caller.

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
`gdr_waiter_only_eg_task` (`ci/fixture/freertos/profiles/main.c`) blocks forever on an
event group whose handle lives only on its own stack, so that group is
discoverable *only* through the waiter channel -- the live no-ghost assertion
now permits exactly one host (that anonymous event group) that no earlier
channel finds, and would fail if the container_of probe fabricated objects out
of plausible neighbouring memory beyond it.
