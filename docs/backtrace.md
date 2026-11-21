# Task Backtrace

Design, unwind rules, cross-stack verification gates, and target firmware
build requirements for `rtt backtrace`, `frt backtrace`, and `gdr api backtrace`.
For module inventories see [`architecture.md`](architecture.md); for RTOS/ISA
matrices and exclusions see [`boundary.md`](boundary.md); for fixture lanes and
acceptance tests see [`testing.md`](testing.md).

## End-to-End Task Backtrace Flow

GDR unwinds halted targets non-intrusively without inferior function calls,
memory writes, or raw stack scanning for candidate return addresses. Every task
backtrace runs through three sequential phases.

### 1. Seed, ABI, and Stack-Segment Validation

Before stepping any caller frame, the adapter resolves the target task or
current halted context (`<rtos>/backtrace_gdb.py`) and validates the seed
([`core/unwind.py`](../core/unwind.py)):

1. **RTOS identity, version, and struct layout**: Verified from explicit
   `gdr init <rtos> <version>` against ELF symbols and `-g3` DWARF macros;
   kernel feature toggles and TCB offsets derive from ELF DWARF (`<rtos>/layout.py`).
2. **ISA / ABI & port/scheduler verification**: Reads ELF CPU attributes
   ([`core/cpus.py`](../core/cpus.py)) and inspects compiled port, scheduler,
   and trap routines (`<rtos>/stack_frame_profile.py`,
   `<rtos>/*_scheduler_switch.py`) rather than guessing from application caller
   code. Saved contexts decode only verified layouts (`<rtos>/backtrace.py`).
3. **Seed & segment bounds**:
   - Enforces recorded downward-growing task stack bounds
     (`configRECORD_STACK_HIGH_ADDRESS` on FreeRTOS; `stack_addr`/`stack_size` on
     RT-Thread) via typed `StackSegment` objects (`stack-bounds-unavailable`
     when absent).
   - Validates `seed.pc > 0` inside ELF `SHF_EXECINSTR` ranges and
     `low <= seed.sp <= high` (or refuses unstarted tasks unless TCB/entry/fill
     or verified first-start/switch-target evidence holds).
   - If halted directly at an exception entry (`seed.at_exception_entry`),
     runs `exception_hook` immediately under the exception budget before entering
     the ordinary frame loop.

```text
rtt/frt backtrace <task> / gdr api backtrace -> adapter (backtrace_gdb.py)
  -> saved task profile (backtrace.py + stack_frame_profile.py)
     or live/handler/scheduler seed (scheduler_seed.py / unwind_arm.py / unwind_riscv.py)
  -> core.unwind_meta (ELF sections, sized STT_FUNC symbols, code bytes)
  -> step order: .ARM.exidx (EHABI) -> DWARF CFI -> leaf instruction proof -> FP fallback
  -> core.unwind (PC - 1 normalization, exception hooks, bounds, budgets)
  -> frames + method + complete/stop_reason + messages
```

### 2. Per-Frame Step and Fallback Order

At each frame (`lookup_pc`), [`core/unwind.py`](../core/unwind.py) tries
unwinding mechanisms in strict priority order:

1. **ARM `.ARM.exidx` / `.ARM.extab` (EHABI) first** *(ARM ELFs only)*:
   - Matches only when an `.ARM.exidx` entry pairs with an enclosing sized
     `STT_FUNC` symbol (`(entry.start & ~1) == symbol.start`); entries never
     inherit across uncovered gaps.
   - Marks `has_metadata = True`. Because EHABI encodes a single post-prologue
     program per function, if `lookup_pc == symbol.start` and EHABI computes
     `new_sp != sp`, it logs `ehabi-prologue-unexecuted` and defers to per-PC
     DWARF CFI.
2. **DWARF CFI second**:
   - Evaluates `.debug_frame`/`.eh_frame` (CIE versions `1`, `3`, and `4`) with
     on-demand `PT_GNU_EH_FRAME`/`.eh_frame_hdr` binary search (falling back to
     linear `.eh_frame` parsing on index errors) and bounded integer `DW_CFA_*`
     expressions (`DW_OP_stack_value` terminal only; `DW_OP_reg*` rejected).
   - Marks `has_metadata = True` whenever an FDE row or metadata parse error is
     present.
3. **Instruction-proven `leaf` return third**:
   - Runs **only** on a clean metadata gap (`not has_metadata`, or
     `ehabi-prologue-unexecuted` without parse failures) when `.ARM.exidx` is
     structurally intact.
   - Decodes bounded straight-line instructions inside the sized `STT_FUNC`
     function (`core/unwind_leaf.py`) to prove SP preservation and return
     through `LR`/`ra`, stripping any clobbered registers so stale values are
     not reused by callers.
4. **Frame-pointer (`FP`) fallback last — strictly gap-only**:
   - Runs **only** when `outcome is None`, `allow_frame_pointer` is enabled,
     and `not has_metadata` (disabled unconditionally on Armv7-A).
   - **No fallback after metadata failure**: whenever EHABI or CFI metadata
     covered `lookup_pc` (`has_metadata == True`) and failed (corrupt table,
     unsupported CFA rule, expression error/budget exhaustion), `FP` fallback
     is prohibited and unwinding stops immediately with that metadata error.

### 3. Cross-Stack Transitions, PC Normalization, and Invariants

After recovering `(new_regs, new_sp, raw_caller)` from a step:

- **Exception/trap crossing**: If `exception_detector(raw_caller)` matches
  (such as Arm M-profile `EXC_RETURN`), `_apply_transition` invokes the
  architecture `exception_hook` (`core/unwind_arm.py` or `core/unwind_riscv.py`)
  to validate the exception frame and switch back to the interrupted segment
  under independent `max_exception_depth` and `max_exception_bytes` budgets.
- **PC lookup normalization**: Ordinary call sites look up caller unwind info at
  `caller_pc - 1`; seeds, signal CIE frames, crossed exception PCs, and proven
  task roots retain exact PCs.
- **Safety checks**: Every step requires `caller_lookup_pc` inside executable ELF
  code ranges (`is_code`), monotonic stack progression (`new_sp >= sp`), valid
  segment bounds (`low <= new_sp <= high`), unique `(segment, pc, sp)` state
  (`loop` rejection), and depth within `max_depth`.

## Cross-Stack Exception, Fault, and Scheduler Gates

### Arm M-Profile IRQ, Fault, and Vector Resolution

Cross-stack M-profile unwinding (`core/unwind_arm.py`) requires readable
`xPSR`/`MSP`/`PSP`, proven running-scheduler task ownership, resolved MSP
bounds, and external vector capacity checks:

- **MSP bounds resolution**: Resolves interrupt stack bounds from standard
  linker symbols (`__StackLimit`..`__StackTop`, `_sstack`..`_estack`), dedicated
  ELF stack sections (`.irq_stack`, `.isr_stack`, `.stack`), or the vector-table
  initial MSP (`__gdr_irq_stack_low` and `__gdr_irq_stack_top` override).
- **Vector capacity & VTOR**:
  - **Armv6-M** requires Cortex-M0 `CPUID`, fixed-zero vector base without
    `ICTR`/`VTOR`/`CCR`, and derives external IRQ capacity from `.isr_vector`,
    `__Vectors_End`, or the first code symbol after `0x0` (`__gdr_vector_table_end`
    and `__gdr_armv6m_irq_count` override). Cortex-M0+ and relocated VTOR are
    unverified.
  - **Armv7-M / Armv7E-M / Armv8-M Mainline** enforce `ICTR` external vector
    capacity `<= 47` and readable `FPCCR` when decoding extended VFP frames
    (`EXC_RETURN=0xFFFFFFE1/E9/ED`).
- **Exception numbers & faults**:
  - `NMI` and `DebugMonitor` stop with `exception-number-unverified`.
  - Synchronous faults unwind into the faulting task with
    `stop_reason=synchronous-fault` when SCB registers (`CFSR`/`HFSR`/`MMFAR`/`BFAR`)
    prove a precise fault cause; imprecise, stacking, vector-fetch, and
    debug-event faults stop with `fault-frame-imprecise`. `IACCVIOL` has no live
    QEMU fault path.
  - **Armv8-M Mainline** requires explicit FPU/TrustZone/MPU compile macros
    (`port-configuration-unavailable` otherwise). **Armv8-M Baseline**
    (`Cortex-M23`) is unsupported (`unverified-port`).

### RISC-V M-Mode Trap & Scheduler Verification

RISC-V unwinding (`core/unwind_riscv.py`, `<rtos>/riscv_scheduler_switch.py`)
requires direct-mode `mtvec` dispatch (`mstatus.MPP == M`), resolved ISR stack
bounds (`__freertos_irq_stack_top` / `xISRStackTop` /
`configISR_STACK_SIZE_WORDS`), and compiled instruction verification of the
port's trap and context-switch blocks (`scheduler-stage-ambiguous:*` when
unverified).

## Firmware Build & Configuration Requirements

### Mandatory for Unwinding and Verification

1. **Target architecture and ABI flags**: Pass explicit CPU, ISA, and float ABI
   flags (`-mcpu=... -mthumb -mfloat-abi=...` on Arm; `-march=... -mabi=...` on
   RISC-V) so ELF attributes match target calling conventions (`aapcs32`,
   `aapcs32-vfp`, `ilp32`, `lp64`).
2. **DWARF debug & macro information (`-g3 -gdwarf-4`)**:
   - `-g` emits `.debug_frame` (used alongside `.ARM.exidx` and required for
     entry/no-EHABI sites) plus kernel struct debug types.
   - `-g3 -gdwarf-4` emits DWARF-4 `.debug_macro` / `.debug_str` rather than
     legacy DWARF-2 `.debug_macinfo` (which `core/gdb_bridge.py` does not scan)
     so `gdr init` / `gdr probe` can verify kernel version/config macros, and
     keeps `.debug_frame` within supported CIE versions `1`/`3`/`4`.
3. **Unwind tables (`-funwind-tables`)**: Emits `.ARM.exidx`/`.ARM.extab` on Arm
   and `.eh_frame` on RISC-V so every C frame carries deterministic unwind
   metadata.
4. **Unstripped `.elf` & linker section retention**:
   - Keep full debug info and sized `STT_FUNC` symbols in the `.elf` passed to
     GDB (never strip `-s` the GDB `.elf`; flashing a separate `.bin` is fine).
   - Under `-Wl,--gc-sections`, keep unwind sections alive:
     `KEEP(*(.eh_frame))` (and `KEEP(*(.eh_frame_hdr))` if present) on
     RISC-V/CFI targets, and `*(.ARM.exidx*)` with `__exidx_start`/`__exidx_end`
     on Arm.
5. **RTOS stack bounds & IRQ stack symbols**:
   - Enable `configRECORD_STACK_HIGH_ADDRESS = 1` in FreeRTOS
     (`pxEndOfStack`). Downward-growing stacks only (`portSTACK_GROWTH < 0`).
   - Export MSP / ISR stack bounds (linker symbols/sections above on Arm;
     `__freertos_irq_stack_top` / `xISRStackTop` / `configISR_STACK_SIZE_WORDS`
     on RISC-V) for cross-stack IRQ, fault, and trap unwinding.

### Recommended Build Options

- **`-fno-lto`**: Avoids cross-TU inlining/splitting that can erase or alter
  `STT_FUNC` boundaries and compiled port/scheduler verification patterns.
- **`-g3 -gdwarf-4` in `AFLAGS`**: Preserves symbol bounds and any `.cfi_*`
  directives in assembly handlers in `.debug_frame`.
- **`-O0` or `-Og`**: Keeps prologue/epilogue CFI and compiled port routines
  predictable (`-fno-omit-frame-pointer` is optional because metadata unwinds
  first and `FP` runs only on clean metadata gaps).
