# RT-Thread fixture builds

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

Use the [shared CLI](../../README.md) to prepare and run firmware. `build.sh`
is the internal builder: it receives an immutable tagged source checkout,
creates a new private clone, applies the target/version patch set, and
publishes `rtthread.elf` (plus `rtthread.bin` on RISC-V). It never resets or
cleans an existing working tree.

## BSP and patch selection

| Target | ISA | Releases | Variants | BSP source | Unwind coverage |
| --- | --- | --- | --- | --- | --- |
| `cortex-a9` | Armv7-A | `3.1.0–3.1.5, 4.0.0–4.1.1` | default | `bsp/qemu-vexpress-a9` | Saved integer context (`context_gcc.S`), live C EHABI/CFI, initial entry root, `rt_thread_exit` sentinel; no M-profile `sched`/`irq`/`fault` lanes |
| `microbit` | Armv6-M | `4.0.5` | default | `bsp/gdr-microbit` (patch) | `sched` (first-start & PendSV stages), `irq` (single, nested, 4B `STKALIGN`), saved/live Thumb-1, exit sentinel; no M0 fault fixture (`CFSR` and exception hook absent; FreeRTOS `microbit` covers `fault-frame-imprecise`) |
| `mps2-an385` | Armv7-M | `4.0.5` | default | `bsp/gdr-mps2-an385` (patch) | `sched` (PendSV & `rt_hw_context_switch`), `irq` (single, nested, 4B `STKALIGN`, SysTick), `fault` (`UNDEFINSTR`, precise `BusFault`, `MemManage` `DACCVIOL`, HardFault hook trampoline) |
| `mps2-an386` | Armv7E-M | `4.0.5` | `fpu`, `softfp`, `soft` | `bsp/gdr-mps2-an386` (patch + `softfp`/`soft` overlays) | `sched` (CM3 integer on `soft`; CM4F integer+FPU stages on `fpu`/`softfp`), `irq` (single, nested, 4B `STKALIGN`, FP `FPCA` lazy stacking on `fpu`/`softfp`, SysTick), `fault` (`UNDEFINSTR`, precise `BusFault`, `MemManage` `DACCVIOL`, 36B `soft` vs 40B `fpu`/`softfp` trampoline) |
| `mps2-an505` | Armv8-M Mainline | `4.0.5` | default, `fpu` | `bsp/gdr-mps2-an505` (patch) | `sched` (single Secure state CM33 stages), `irq` (single, nested, 4B `STKALIGN`, SysTick; FP lazy stacking in `fpu`), `fault` (`UNDEFINSTR`, precise `BusFault`; MPU load surfaces as BusFault on QEMU AN505); TZ dual-world and nonzero `SPLIM` unsupported |
| — | Armv8-M Baseline | — | — | — | No QEMU Cortex-M23 CPU/machine model; verified in unit tests only |
| `rv32` | RV32 | `4.0.5` | default | `bsp/gdr-rv32` (patch) | Saved/live CFI (`ilp32`), M-mode `trap_entry` cross-stack (E310 `deferred-mepc`; nested `>1` fails `nested-trap-epc-unproven`), `rt_hw_context_switch_to`, `rt_hw_context_switch`, `trap_entry` epilogue switches, and `spurious_interrupt` / `exit` stages; no vendor IRQ/fault extensions (XuanTie, Nuclei ECLIC, WCH PFIC) |
| `rv64` | RV64 | `4.0.4, 4.0.5, 4.1.0, 4.1.1` | default | `bsp/qemu-riscv-virt64` (`4.0.4–4.1.0`), `bsp/qemu-virt64-riscv` (`4.1.1`) | Saved/live CFI (`lp64`), M-mode `trap_entry` (`stacked-epc`), `rt_hw_context_switch_to`, `rt_hw_context_switch`, `trap_entry` epilogue switches, and `rt_hw_context_switch_exit` stages; no vendor IRQ/fault extensions |

Patch series live under `patches/<target>/<version>/`. A patch must apply
cleanly to its own BSP and toolchain; do not share patches between the
Cortex-A9 and RV64 directories without verifying both. Shared coverage and matrix
policy are documented in [testing](../../../docs/testing.md).

## Build dependencies

The CLI registers `arm-none-eabi-*` for Arm targets and `riscv-none-elf-*` for RISC-V.
The RV64 BSP expects `riscv64-unknown-elf-*` aliases; the shared container
creates them in its compiler distribution. Both compilers must provide
newlib headers. Modern releases use Python 3/SCons 4; 3.1.x uses an isolated
Python 2/SCons 3 installation linked under the host tool cache.

Every shared-runner build enables `-g3 -gdwarf-4` and publishes a
`.debug-macros` marker alongside firmware so version-probe tests require
the corresponding macro evidence. GDB reads the ELF; the RISC-V QEMU machine
boots its raw BIN sibling as BIOS. Build logs and patched private checkouts
remain under the shared cache for inspection.

## `cortex-a9` (Armv7-A)

```bash
python3 ci/test.py run rtthread cortex-a9 4.0.5
python3 ci/test.py matrix smoke
```

### RTOS config

Uses upstream `bsp/qemu-vexpress-a9` across `3.1.0–3.1.5` and `4.0.0–4.1.1`
(4.x tracks SMP globals in `_cpus`, while 3.1.x uses single-core
`rt_current_thread`). Patch sets pin `RT_NAME_MAX=16` so canonical IPC and
worker thread names stay untruncated; RT-Thread 3.1.4 layers its messagequeue
sender-wait fixture onto the 3.1.x baseline, whereas 3.1.0–3.1.3 omit the
sender-wait API. The lane has no M-profile NVIC, PendSV, or fault handler
overlays.

### Build option

Built via SCons with `arm-none-eabi-*`, `-march=armv7-a -marm -msoft-float`,
`-Wall -Wno-error=implicit-function-declaration`, `-g3 -gdwarf-4` (plus
`-funwind-tables -fno-omit-frame-pointer` in 4.0.4+ backtrace fixture
patches). Publishes `rtthread.elf` and `.debug-macros`. Releases `3.1.0–3.1.5`
run in an isolated Python 2 / SCons 3 environment.

### QEMU boot

`qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` without semihosting;
serial readiness string is `"GDR test fixture ready."` and GDB uses `arm`.

### Unwind testcase

Covers saved integer thread context (`context_gcc.S`), live C EHABI/CFI
through the `gdr_bt_live_middle` -> `gdr_bt_live_leaf` (`gdr_bt_live_stop`)
chain (4.0.4+), unstarted-thread entry (`gdr_unstarted`), `rt_thread_exit`
stop sentinel, and omitted-target live unwind (`rt bt`). Acceptance and runtime
limits live in [testing](../../../docs/testing.md#rt-thread-suite-organization-and-capability-coverage)
and [boundaries](../../../docs/boundary.md#rt-thread-backtrace-matrix).

## `microbit` (Armv6-M)

```bash
python3 ci/test.py run rtthread microbit 4.0.5
```

### RTOS config

`patches/microbit/4.0.5/` adds `bsp/gdr-microbit` (`RT_NAME_MAX=20`,
`RT_TICK_PER_SECOND=100`). nRF51 TIMER2 at `0x4000A000` drives ticks via
IRQ10 (exception 26): a 16 MHz source divided by prescaler 4 (1 MHz) with
`CC0=10000` and the compare-to-clear shortcut; SysTick registers remain unused.
UART0 provides readiness serial output. Flash is 256 KiB at `0x0` and RAM is
expanded to 256 KiB at `0x20000000`, reserving a 64 KiB heap and a disjoint
16 KiB `.irq_stack` MSP section (`__StackLimit`/`__StackTop`). nRF51 TIMER0
(`0x40008000`, IRQ8, priority `0xC0`) and TIMER1 (`0x40009000`, IRQ9, priority
`0x40`) drive external IRQ tests.

### Build option

Built via SCons with `arm-none-eabi-*` and
`-mcpu=cortex-m0 -mthumb -mfloat-abi=soft -O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer`.
`003-port-compat.patch` updates `libcpu/arm/cortex-m0/context_gcc.S` so PendSV
constructs `0xFFFFFFFD` for Thread/PSP first-start returns, keeps the initial
hardware PC halfword-aligned, and retains the Thumb state bit on normal
task-exit LR while preserving upstream's 64-byte saved integer layout without
adding CFI directives to `libcpu`.

### QEMU boot

`qemu-system-arm -M microbit -global nrf51-soc.sram-size=262144 -kernel rtthread.elf`
without semihosting (`arm` architecture in GDB; `"GDR test fixture ready."` on
UART0).

### Unwind testcase

- **Scheduled & saved**: `sched` stage ownership across first-start and
  PendSV (`test_backtrace.py`), blocked-thread saved Thumb-1 frames,
  and `rt_thread_exit` stop sentinel.
- **External IRQ (`004-irq-fixture.patch`)**: `GDR_IRQ_SCENARIO_SINGLE`,
  `GDR_IRQ_SCENARIO_NESTED` (TIMER1 preempting TIMER0 across MSP), and
  `GDR_IRQ_SCENARIO_ALIGN` (4-byte SP alignment padding / `STKALIGN`).
- **Fault gap**: No fault fixture on `microbit`; the Armv6-M port lacks
  `rt_hw_exception_install` and `CFSR` status registers. The `fault-frame-imprecise`
  refusal is covered by FreeRTOS `microbit`. Raw compatibility checks live in
  [testing](../../../docs/testing.md#rt-thread-suite-organization-and-capability-coverage).

## `mps2-an385` (Armv7-M)

```bash
python3 ci/test.py run rtthread mps2-an385 4.0.5
```

### RTOS config

`patches/mps2-an385/4.0.5/` adds `bsp/gdr-mps2-an385` (`RT_NAME_MAX=20`,
`RT_TICK_PER_SECOND=100`, 32-entry vector table). CMSDK UART0 at `0x40004000`
prints readiness output; SysTick runs off the 25 MHz board clock at 100 Hz.
Linker maps 4 MiB flash at `0x0` and 4 MiB RAM at `0x20000000`, with a 64 KiB
heap and a non-overlapping 16 KiB MSP section exporting `__StackLimit`/`__StackTop`.
CMSDK Timer0 (`0x40000000`, IRQ8) and Timer1 (`0x40001000`, IRQ9) generate
IRQ test exceptions. Has no FPU, TrustZone, or SMP.

### Build option

Compiles unmodified upstream `libcpu/arm/cortex-m3/context_gcc.S` and
`cpuport.c` with `arm-none-eabi-*` and
`-mcpu=cortex-m3 -mthumb -mfloat-abi=soft -O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer`
against newlib/nosys and `libgcc` EHABI personality routines. `003-irq-fixture.patch`
and `004-fault-fixture.patch` layer the runtime-gated exception fixtures cleanly on
top of the base BSP.

### QEMU boot

`qemu-system-arm -M mps2-an385 -kernel rtthread.elf` without semihosting
(`arm` in GDB).

### Unwind testcase

- **Scheduled & saved**: PendSV / `rt_hw_context_switch` stage ownership,
  blocked integer threads, `gdr_unstarted`, and `gdr_bt_live_stop` 3-frame
  live chain.
- **IRQ (`003-irq-fixture.patch` + SysTick)**: `GDR_IRQ_SCENARIO_SINGLE`,
  `GDR_IRQ_SCENARIO_NESTED`, `GDR_IRQ_SCENARIO_ALIGN`, and SysTick
  `rt_tick_increase` cross-stack unwind into the interrupted task.
- **Fault (`004-fault-fixture.patch`, `gdr_fault_gate`)**: `gdr_fault` thread cycles
  `GDR_FAULT_SCENARIO_UNDEFINSTR` (escalated UsageFault `UNDEFINSTR`),
  `GDR_FAULT_SCENARIO_BUS` (`0x50000000` precise BusFault), and
  `GDR_FAULT_SCENARIO_MEMMANAGE` (`0x60000000` MPU `DACCVIOL` + `MMARVALID`)
  at `gdr_fault_*_stop` labels. The `rt_exception_hook` receives `struct exception_info`,
  compensates `libcpu`'s 9-word (36-byte) software push below the hardware
  frame before returning via PSP, advances PC past the faulting instruction,
  clears sticky `CFSR`/`HFSR` bits, and keeps UsageFault disabled so escalation
  to HardFault exercises `synchronous-fault` trampoline unwinding.

## `mps2-an386` (Armv7E-M)

```bash
python3 ci/test.py run rtthread mps2-an386 4.0.5 fpu
python3 ci/test.py run rtthread mps2-an386 4.0.5 softfp
python3 ci/test.py run rtthread mps2-an386 4.0.5 soft
```

### RTOS config

`patches/mps2-an386/4.0.5/` adds `bsp/gdr-mps2-an386` (`RT_NAME_MAX=20`,
`RT_TICK_PER_SECOND=100`). Memory map, CMSDK UART0 (`0x40004000`), 25 MHz
SysTick, 64 KiB heap, 16 KiB MSP span, and CMSDK Timer0/Timer1 match AN385;
`board.c` enables `CPACR` CP10/CP11 because upstream M4 init leaves FPU
access disabled. Variant overlays under `patches/mps2-an386/4.0.5/softfp/` and
`patches/mps2-an386/4.0.5/soft/` adjust the compiler float ABI (and `soft` skips
`GDR_IRQ_SCENARIO_FP` and uses the 36-byte HardFault PSP unwind offset).

### Build option

Compiles upstream `libcpu/arm/cortex-m4/` with
`-mcpu=cortex-m4 -mthumb` plus `-mfloat-abi=hard -mfpu=fpv4-sp-d16` (`fpu`),
`-mfloat-abi=softfp -mfpu=fpv4-sp-d16` (`softfp`), or `-mfloat-abi=soft` (`soft`),
and `-O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer`.
`003-fpu-context.patch` wraps upstream conditional `VSTMDBEQ`/`VLDMIANE`/`MOVEQ`/`BICNE`
instructions in Thumb-2 `IT` blocks and adds `worker_fpu`; `004-irq-fixture.patch`
and `005-fault-fixture.patch` add the runtime-gated exception fixtures.

### QEMU boot

`qemu-system-arm -M mps2-an386 -kernel rtthread.elf` without semihosting
(`arm` in GDB).

### Unwind testcase

- **Scheduled & FPU (`fpu` / `softfp` / `soft`)**: CM4F integer+FPU PendSV stages and
  `worker_fpu` extended saved slots (`FPCA` / `s16-s31`) on `fpu` (`aapcs32-vfp`) and
  `softfp` (`aapcs32`), plus CM4 integer PendSV stages (`__SOFTFP__`, 9-word save) on `soft`.
- **IRQ (`004-irq-fixture.patch` + SysTick)**: `GDR_IRQ_SCENARIO_SINGLE`,
  `GDR_IRQ_SCENARIO_NESTED`, `GDR_IRQ_SCENARIO_ALIGN`, `GDR_IRQ_SCENARIO_FP`
  (`EXC_RETURN=0xffffffed` lazy FPU stacking across MSP on `fpu`/`softfp`), and SysTick
  `rt_tick_increase` cross-stack unwind.
- **Fault (`005-fault-fixture.patch`, `gdr_fault_gate`)**: `GDR_FAULT_SCENARIO_UNDEFINSTR`,
  `GDR_FAULT_SCENARIO_BUS`, and `GDR_FAULT_SCENARIO_MEMMANAGE` (`DACCVIOL`);
  `rt_exception_hook` receives the hardware frame pointer and adds back the
  10-word (40-byte) M4F software prologue on `fpu`/`softfp` (`r4-r11` + `EXC_RETURN` + FPU flag word)
  or the 9-word (36-byte) integer prologue on `soft`.

## `mps2-an505` (Armv8-M Mainline)

```bash
python3 ci/test.py run rtthread mps2-an505 4.0.5
python3 ci/test.py run rtthread mps2-an505 4.0.5 fpu
```

### RTOS config

`patches/mps2-an505/4.0.5/` adds `bsp/gdr-mps2-an505` (`RT_NAME_MAX=20`,
`RT_TICK_PER_SECOND=100`). Runs in single Secure state (`ARM_CM33_ENABLE_TRUSTZONE`
absent, security-context calls compile as stubs). Image links in Secure SSRAM
alias at `0x10000000` (4 MiB flash) and RAM at `0x28000000` (2 MiB), with a
64 KiB heap and disjoint 16 KiB `.irq_stack` MSP span. CMSDK UART0 Secure alias
(`0x50200000`) prints readiness output; SysTick runs off a 20 MHz clock at
100 Hz. External IRQ timers use Secure APB Timer0/1 (`IRQ19`/`IRQ20`
exceptions).

### Build option

Base compiles upstream `libcpu/arm/cortex-m33/` with
`-mcpu=cortex-m33 -mthumb -mfloat-abi=soft -O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer`.
`003-port-compat.patch` sets assembly CPU to Cortex-M33, clears the Thumb bit on
the initial hardware PC, and constructs `0xFFFFFFFD` for the first Thread/PSP
return without adding saved slots or CFI directives to `libcpu`; `004-irq-fixture.patch`
and `005-fault-fixture.patch` add the runtime-gated IRQ and fault threads. The
`fpu` overlay switches to `-mfloat-abi=hard -mfpu=fpv5-sp-d16`, enables CP10/CP11
in board init, wraps `VSTMDBEQ`/`VLDMIAEQ` in `IT eq`, and adds `worker_fpu`.

### QEMU boot

`qemu-system-arm -M mps2-an505 -kernel rtthread.elf` without semihosting
(`arm` in GDB).

### Unwind testcase

- **Scheduled & saved**: CM33 single-Secure-state PendSV/switch stages,
  `gdr_unstarted`, live 3-function chain (`gdr_bt_live_stop`), and `fpu`
  extended frame checks when built with `fpu`.
- **IRQ (`004-irq-fixture.patch` + SysTick)**: `GDR_IRQ_SCENARIO_SINGLE`,
  `GDR_IRQ_SCENARIO_NESTED`, `GDR_IRQ_SCENARIO_ALIGN`, SysTick cross-stack,
  and conditional `GDR_IRQ_SCENARIO_FP` under `fpu` (`__VFP_FP__`).
- **Fault (`005-fault-fixture.patch`, `gdr_fault_gate`)**: `GDR_FAULT_SCENARIO_UNDEFINSTR` and
  `GDR_FAULT_SCENARIO_BUS` (`0xA0000000`); M33 hook compensates `libcpu`'s
  13-word (52-byte) software block (`r4-r11`, TrustZone/LR metadata).
  QEMU AN505 surfaces MPU-blocked loads as precise BusFault rather than MemManage
  `DACCVIOL` (live `DACCVIOL` proof remains on AN385/AN386; see
  [boundaries](../../../docs/boundary.md#rt-thread-backtrace-matrix)).

## `rv32` (RV32)

```bash
python3 ci/test.py run rtthread rv32 4.0.5
```

### RTOS config

`patches/rv32/4.0.5/` adds `bsp/gdr-rv32` (`RT_NAME_MAX=20`,
`RT_TICK_PER_SECOND=100`) selecting unmodified `libcpu/risc-v/common/cpuport.c`,
`context_gcc.S`, and the E310 trap entry. Uses 16550 UART at `0x10000000` and
10 MHz CLINT at `0x02000000` (100 ticks/second; RV32 `mtime` uses high/low/high
consistency reads and `mtimecmp` uses low-all-ones/high/low ordering). RAM maps
16 MiB at `0x80000000`, reserving a 64 KiB heap and a 16 KiB interrupt stack.
Static stacks and allocations are 16-byte aligned.

### Build option

Built via SCons with `riscv-none-elf-*`,
`-march=rv32imac_zicsr -mabi=ilp32 -mcmodel=medany -O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer -ffunction-sections -fdata-sections -fno-common -fno-builtin -msmall-data-limit=0`.

### QEMU boot

`qemu-system-riscv32 -M virt -cpu rv32 -m 256M -bios rtthread.bin`; GDB loads
`rtthread.elf` (`riscv:rv32`).

### Unwind testcase

Covers 32-bit pointer width, saved/live `ilp32` CFI, `gdr_unstarted`, live
3-function worker chain (`gdr_bt_live_middle` -> `gdr_bt_live_leaf` with
`gdr_irq_hold` spin gate), M-mode CLINT `trap_entry` -> `handle_trap` ->
`rt_tick_increase` cross-stack unwind, `rt_hw_context_switch_to`,
`rt_hw_context_switch`, `trap_entry` epilogue switch windows, `spurious_interrupt` /
`rt_hw_context_switch_exit`, and fail-closed `nested-trap-epc-unproven` under E310
nesting. Vendor IRQ/fault extensions (XuanTie, Nuclei ECLIC, WCH PFIC) are not
supported or built. Trap unwinding scope lives in
[boundaries](../../../docs/boundary.md#rt-thread-backtrace-matrix).

## `rv64` (RV64)

```bash
python3 ci/test.py matrix rtthread-rv64
```

### RTOS config

Uses upstream `bsp/qemu-riscv-virt64` (`4.0.4`, `4.0.5`, `4.1.0`) and
`bsp/qemu-virt64-riscv` (`4.1.1`) with upstream `RT_NAME_MAX=20`. Exercises the
16550 UART and 10 MHz CLINT on QEMU `virt`; 3.1.x has no upstream RV64 BSP.

### Build option

Built via SCons with `riscv-none-elf-*` (aliased as `riscv64-unknown-elf-*`
in shared containers) for `rv64gc` / `lp64`, `-g3 -gdwarf-4`, publishing both
`rtthread.elf` and `rtthread.bin`.

### QEMU boot

`qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin`; GDB loads
`rtthread.elf` (`riscv:rv64`).

### Unwind testcase

Covers 64-bit pointer width, saved/live `lp64` CFI, IPC blocking/unstarted
threads, `gdr_bt_live_middle` -> `gdr_bt_live_leaf` (`gdr_irq_hold` spin gate),
M-mode CLINT `trap_entry` -> `handle_trap` -> `rt_tick_increase` cross-stack unwind,
and `rt_hw_context_switch_to` / `rt_hw_context_switch` / `trap_entry` epilogue /
`rt_hw_context_switch_exit` scheduler stages. Vendor IRQ/fault extensions are
unsupported.
