# Boundaries and Limits

Operational boundaries, RTOS/ISA support matrices, and functional limits of
GDR's non-intrusive debugging model. Development rules live in
[`AGENTS.md`](../AGENTS.md); acceptance profiles and fixture setups live in
[`testing.md`](testing.md).

## General

### Non-intrusive DWARF inspection

GDR inspects halted targets only: it never drives the target, invokes inferior
calls, writes target memory, or scans raw stacks for candidate return
addresses. Because targets are read in a stopped snapshot, interrupted kernel
state updates can appear transient in diagnostic output.

Adapters derive versions, type widths, config features, and struct layouts from
ELF DWARF and `-g3` macro tables rather than `.config` parsing or version
strings. Stripped ELF images or halts outside macro scope degrade affected
probes to safe defaults with warnings instead of guessing.

### SMP core context

Core assignments and per-core pointers come from shared kernel memory
(`pxCurrentTCBs[]` on FreeRTOS, `rt_cpu_index` on RT-Thread) rather than
switching GDB inferiors or per-core register banks. Tasks actively executing on
cores other than the halted GDB core always fail backtrace with
`smp-no-context`.

### Stack direction and bounds

Both adapters support downward-growing stacks only and require recorded stack
bounds (`configRECORD_STACK_HIGH_ADDRESS` on FreeRTOS; `stack_addr`/`stack_size`
on RT-Thread) for stack usage metrics and unwinding (`stack-bounds-unavailable`
when absent).

### Task backtraces

`EXIDX` entries must match an enclosing sized `STT_FUNC` symbol and do not
inherit across uncovered gaps. DWARF CFI supports `.debug_frame`/`.eh_frame`,
on-demand `PT_GNU_EH_FRAME`/`.eh_frame_hdr` search (falling back to `.eh_frame`
on index errors), bounded integer `DW_CFA_*` expressions (`DW_OP_stack_value`
terminal only; `DW_OP_reg*` rejected), and encoded pointers with explicit
section/memory context. Frame-pointer fallback is prohibited whenever CFI or
`EXIDX` metadata fails to parse or an expression errors, and is disabled on
Armv7-A. Clean uncovered PCs admit a `leaf` step only when sized ELF code
proves a straight-line SP/LR-preserving return.

M-profile IRQ unwinding requires readable `xPSR`/`MSP`/`PSP`, running-scheduler
ownership, linker/vector MSP bounds, external vector capacity `<= 47`, `FPCCR`
for extended VFP frames (`EXC_RETURN=0xFFFFFFE1/E9/ED`), and rejects
`NMI`/`DebugMonitor` (`exception-number-unverified`). Armv6-M requires Cortex-M0
`CPUID` and fixed-zero vectors without `ICTR`/`VTOR`/`CCR`. Imprecise,
stacking, vector-fetch, and debug-event faults refuse (`fault-frame-imprecise`);
`IACCVIOL` has no live QEMU fault path. Armv8-M profile selection requires
explicit FPU/TrustZone/MPU compile macros (`port-configuration-unavailable`
otherwise). **Armv8-M Baseline** (`Cortex-M23`) is unsupported
(`unverified-port`).

RISC-V trap and scheduler unwinding requires compiled direct-mode `mtvec`
dispatch (`mstatus.MPP == M`), resolved IRQ stack bounds, and compiled
instruction verification (`scheduler-stage-ambiguous:*` on unverified PCs).

## RT-Thread

### RT-Thread version and ISA scope

Accepts versions `3.1.0–3.1.5` and `4.0.0–4.1.1` across **Armv6-M**, **Armv7-M**,
**Armv7E-M**, **Armv8-M Mainline**, **Armv7-A**, **RV32**, and **RV64**: `3.1.x`
covers Armv7-A (`cortex-a9`, where `ObjectType` `Null` exists only prior to
`3.1.3`) and lacks verified saved-context unwinding beyond `stackpc` or RV64
support; `4.0.x–4.1.1` cover Armv6-M/Armv7-M/Armv7E-M/Armv8-M Mainline/Armv7-A,
RV32 (`4.0.5`), and RV64 (`4.0.4–4.1.1`). Armv8-M Baseline is unsupported.

Version verification checks explicit `X.Y.Z` input against the supported
ranges, confirms target presence via RT-Thread core symbols (overridable only with
`--allow-unverified`), and probes target version constants in `rt_thread_init`'s
compilation unit (`RT_VERSION`/`RT_SUBVERSION`/`RT_REVISION` triplet first to
cover `3.1.x`, then packed-hex/decimal `RT_VER_NUM`, `RTTHREAD_VERSION`, or
`gdr_rtthread_version_num`). Any detected mismatch refuses initialization
(`version_mismatch`); if markers match without exported version macros,
`gdr probe` reports `undetected` and `gdr init` accepts the user version with an
unverified-version warning.

### Probed heap managers

Because `small_mem`, `slab`, and `memheap` use distinct block headers, heap
walks dispatch from the probed heap manager type and render missing counters as
`N/A` rather than `0`.

### RT-Thread backtrace matrix

| ISA / Target | Saved / Live task | Unstarted task | IRQ / Trap cross-stack | Fault cross-stack | Scheduler transition stop | SMP / MPU / FPU | Primary exclusion / gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Armv6-M (`microbit`, `4.0.5`) | Integer Thumb-1 (`EHABI`/`CFI`/`leaf`) | Saved + `first-start` / PendSV restore | Single, nested, SysTick, 8B-aligned | Unsupported | `rt_hw_context_switch_to`, PendSV stages, `switch-return` | — | Cortex-M0+ & relocated VTOR unverified |
| Armv7-M (`mps2-an385`, `4.0.5`) | Integer (`EHABI`/`CFI`) | Saved + `first-start` / PendSV restore | Single, nested, SysTick, `STKALIGN` | Precise CFSR/HFSR + `HardFault_Handler` trampoline | First-start, PendSV save/restore, `switch-return` | — | Vector capacity `<=47` |
| Armv7E-M (`mps2-an386`) & Armv8-M (`mps2-an505`) FPU | Basic + 204B/216B FPU; CM33 `tz=0`/`PSPLIM=0`/priv `CONTROL` | Saved + `first-start` / PendSV restore | Single, nested, SysTick, extended FPU lazy stacking | Precise CFSR/HFSR + trampoline (`DACCVIOL` unavailable on AN505) | Verified M4F/CM33 first-start, PendSV, `switch-return` | FPU | Non-secure TZ returns, `PSPLIM != 0`, unprivileged contexts refused |
| Armv7-A (`cortex-a9`, `4.0.5`) | Integer SMP/signal/LWP (`EHABI`; no FP fallback) | Saved context only | Refused | Refused | Unsupported | Kernel LWP context | `3.1.x` has no verified saved-context layout beyond `stackpc` |
| RV32 (`E310`, `4.0.5`) & RV64 (`virt64`, `4.0.4–4.1.1`) | Integer `ilp32`/`lp64` (`CFI`/`leaf`) | Saved + switch / trap / restore stages | M-mode direct `mtvec` (`E310` `deferred-mepc`; `virt64` `stacked-epc`) | Unsupported (`exception-model-unverified`) | `rt_hw_context_switch_to`, `rt_hw_context_switch`, `trap_entry` prologue/epilogue (`from`/`to`), `exit`/`spurious_interrupt` | — | `E310` nested trap `>1` (`nested-trap-epc-unproven`), `RT_USING_SMP`, `rt_signal_check`, RV FPU, vendor IRQs refused |

Because `rt_thread.entry` persists in the TCB and `rt_hw_stack_init` fills
initial callee-saved register slots with `0xDEADBEEF`, `source=unstarted` is
proven whenever saved or restored `PC == entry` and those initial callee-saved
register slots still hold `0xDEADBEEF` (without this register check, a running
thread whose PC happens to be at `entry` would be misclassified as unstarted).
On RISC-V E310 (`rv32`), `mepc` is not saved on prologue entry, so nested
interrupts (`rt_interrupt_nest > 1` in dispatch or `> 0` in
prologue/epilogue/`spurious_interrupt`) overwrite `mepc` and fail closed with
`nested-trap-epc-unproven`.

## FreeRTOS

### FreeRTOS version and ISA scope

Accepts versions `10.3.0–11.3.x` across **Armv6-M** (`ARM_CM0`), **Armv7-M**
(`ARM_CM3`), **Armv7E-M** (`ARM_CM4F`), **Armv8-M Mainline** (`ARM_CM33_NTZ`,
dual-core SMP, and MPU wrappers v2), **RV32**, and **RV64** (where `TickType_t`
matches pointer width and event-group control bits occupy the top byte). Armv8-M
Baseline and grow-up stacks (`portSTACK_GROWTH > 0`) are unsupported.

Version verification checks explicit `X.Y.Z` input against `10.3.0–11.3.x`,
confirms target presence via FreeRTOS kernel symbols (overridable only
with `--allow-unverified`), and reads target version constants via `-g3` macro
info in `vTaskStartScheduler`'s compilation unit
(`tskKERNEL_VERSION_MAJOR/_MINOR/_BUILD` triplet first, then
`tskKERNEL_VERSION_NUMBER`, then `gdr_freertos_version_num`). Any detected
mismatch refuses initialization (`version_mismatch`); if markers match without
readable macros, `gdr probe` narrows only the layout era (`V11 SMP-era` vs.
`pre-SMP V10.x` via `xTaskRunState` / `pxCurrentTCBs`) and `gdr init` accepts
the user version with an unverified-version warning.

### Task `Entry` column and stack metrics

`tskTaskControlBlock` discards the task entry pointer after creation, so task
tables omit `Entry`. `Stack`, `Used`, and `HighWater` require
`configRECORD_STACK_HIGH_ADDRESS` (`pxEndOfStack`, inclusive of one
`StackType_t`); `HighWater` reports maximum bytes *used* (`max(Used, recorded
span - untouched 0xa5 fill)` including saved-SP slots) rather than FreeRTOS's
native remaining words. Under MPU wrappers v2, `pxTopOfStack` addresses the TCB
context array rather than the task stack, so `Used` reads the decoded task SP
while fill scanning uses the recorded stack span. Unfilled, exhausted, or
partially read stacks omit affected values.

### Heap inspection limits

`BlockLink_t` carries no owner field, so `frt heap` cannot attribute heap usage
per task; `Allocated`/`Freed` report cumulative successful operation counts.
`heap_4`/`heap_2` linear walks begin from `align_up(&ucHeap,
portBYTE_ALIGNMENT)` (skipping when `ucHeap` is absent) because allocations grow
below the free-list head and `heap_2` sorts by size. `heap_5` skips linear
walks unless `configENABLE_HEAP_PROTECTOR` exports region bounds (`TotalSize`
requires a single contiguous region reaching `pxEnd`). Before heap
initialization (`prvHeapInit`), `Heap used` is omitted and only `Heap total` is
shown.

### Diagnostics and `NextUnblockTime`

Automatic tables/summaries emit only `fail:`/`skipped:` verdicts, while
`frt check [scope] [name]` surfaces passes and structural skips.
`NextUnblockTime` checks are skipped before scheduler startup or while
suspended; an earlier cached tick after event unblocking is valid, whereas a
later cached tick reports the delayed task.

### 6 channel object discovery and queue classification

FreeRTOS lacks a global registry for most object kinds, so enumeration merges
five channels in strict priority order while singular lookup supplies `user`:

| Channel | Source & scope | Covers & limits |
| --- | --- | --- |
| `mpu-pool` | `xKernelObjectPool` (`configENABLE_MPU` wrappers v2) | All kinds; skips `0`/`~0` slots, no name, queue slot is inferred until refined |
| `registry` | Full `xQueueRegistry` scan (holes included) | Queue/semaphore/mutex with non-null name & handle; inferred until refined |
| `active` | `pxCurrentTimerList` / `pxOverflowTimerList` | Active timers (`pcTimerName`); stopped and expired one-shots are unlinked |
| `symbol` | Session-cached `info variables` scan | `Static*_t` address or `*Handle_t` value (MPU `1..N` pool index translated); timers prefer `pcTimerName` |
| `waiter` | Live blocked TCB `xEventListItem.pxContainer` (`container_of`) | Queue/semaphore/mutex/event group; excludes scheduler lists and checks structural invariants/`portMAX_DELAY` |
| `user` | Singular `0x`/identifier/decimal argument | All kinds; resolves static/handle symbols (with MPU index translation) before decimal fallback |

Enumeration deduplicates by address: the highest-priority channel keeps the
primary `Src` (used for `frt objects` totals) and name (later channels supply a
name only when none was found, as for `mpu-pool` slots), while corroborating
hits append to `Src` (e.g., `registry+symbol+waiter`). Queue-family candidates
are refined before kind filtering: `ucQueueType` is definitive when
`configUSE_TRACE_FACILITY == 1`; without it, `pcHead`/`uxItemSize` inference
leaves binary vs. counting semaphores and plain vs. recursive mutexes
indistinguishable (`?` marker). Unregistered dynamic objects without global
handles and stopped/expired timers remain unreachable.

Singular commands redirect when `Queue_t` classification proves a mismatch,
never on inferred results (`?` marker). `waiter` is heuristic (checked via
`EventGroupDef_t` control-byte / non-mapped-range guards and `QueueDefinition`
size, `pcHead`, and dual `portMAX_DELAY` sentinels), so every earlier channel
outranks it.

### FreeRTOS backtrace matrix

| ISA / Target | Saved / Live task | Unstarted task | IRQ / Trap cross-stack | Fault cross-stack | Scheduler transition stop | SMP / MPU / FPU | Primary exclusion / gate |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Armv6-M (`microbit`, `ARM_CM0`) | Integer Thumb-1 (`EHABI`/`CFI`/`leaf`) | `first-start` SVC only; PendSV refused | Single, nested, SysTick, 8B-aligned (6-bit IPSR `<=47`) | Refused (`fault-frame-imprecise`) | `ARM_CM0` PendSV, SVC, `vTaskSwitchContext` | — | Cortex-M0+ & relocated VTOR unverified |
| Armv7-M (`mps2-an385`, `ARM_CM3`) | Integer (`EHABI`/`CFI`) | `first-start` SVC only; PendSV refused | Single, nested, SysTick, `CCR.STKALIGN` | Precise `UsageFault`/`BusFault`/`MemManage` | `ARM_CM3` PendSV, SVC, `vTaskSwitchContext` | — | Vector capacity `<=47` |
| Armv7E-M (`mps2-an386`, `ARM_CM4F`) | Basic + 204B FPU (`EXC_RETURN`) | `first-start` SVC only; PendSV refused | Single, nested, SysTick, `0x68`B FPU (`LSPACT=1` integer) | Precise `UsageFault`/`BusFault`/`MemManage` | `ARM_CM4F` basic/FPU PendSV, SVC, `vTaskSwitchContext` | FPU | Missing `FPCCR` -> `exception-fp-state-unavailable` |
| Armv8-M Mainline (`mps2-an505`, `ARM_CM33_NTZ`) | Integer (single-Secure NTZ) | `first-start` SVC only; PendSV refused | Single, nested, SysTick, `STKALIGN` | Precise `UsageFault`/`BusFault` | `ARM_CM33_NTZ` PendSV, SVC, `vTaskSwitchContext` | — | Dual-world TZ, `SecureFault`, FPU/MPU fault variants excluded |
| Armv8-M SMP+MPU (`mps2-an521`) | Integer on selected core | Refused (no scheduler profile) | Unsupported | Unsupported | Unsupported | SMP + MPU v2 | Other cores -> `smp-no-context`; SMP/MPU handlers refused |
| RV32 / RV64 (`qemu-virt-rv32`, `qemu-virt-rv64`) | Integer `ilp32`/`lp64` (`CFI`/`leaf`) | `xPortStartFirstTask` + incoming switch target (`unstarted`) | M-mode direct `mtvec` IRQ + `ecall` yield (`mcause=11`) | Refused (`exception-frame-type-unproven` for non-`ecall`) | `xPortStartFirstTask`, trap prologue, `ecall` yield, `vTaskSwitchContext`, `processed_source` | — | Vendor IRQs/faults, SMP, RV FPU, preempted pre-`ret` first start refused |

Without a retained TCB entry pointer, `source=unstarted` is accepted only inside
verified `first-start` or incoming scheduler selection with ELF entry evidence.
On RISC-V, a started first task begins with `ra == entry`, so ordinary `PC - 1`
unwinding stops at `cfi-stack-overflow` rather than treating `ra` as a caller
(`other` tasks reach `xTaskReturnAddress == 0`). If an interrupt hits inside the
three-instruction `mstatus.MIE` window before `xPortStartFirstTask` `ret`, the
overwritten stack pointer fails closed (`scheduler-first-start-entry-unproven`).
