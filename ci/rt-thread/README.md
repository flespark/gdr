# RT-Thread fixture builds

Use the [shared CLI](../README.md) to prepare and run firmware. `build.sh`
is the internal builder: it receives an immutable tagged source checkout,
creates a new private clone, applies the target/version patch set, and
publishes `rtthread.elf` (plus `rtthread.bin` on RISC-V). It never resets or
cleans an existing working tree.

## BSP and patch selection

| Target | Releases | BSP source |
| --- | --- | --- |
| `cortex-a9` | 3.1.0–3.1.5, 4.0.0–4.1.1 | `bsp/qemu-vexpress-a9` |
| `rv64` | 4.0.4, 4.0.5, 4.1.0 | `bsp/qemu-riscv-virt64` |
| `rv64` | 4.1.1 | `bsp/qemu-virt64-riscv` |
| `mps2-an385` | 4.0.5 | `bsp/gdr-mps2-an385` supplied by fixture patches |
| `microbit` | 4.0.5 | `bsp/gdr-microbit` supplied by fixture patches |
| `mps2-an505` | 4.0.5 | `bsp/gdr-mps2-an505` supplied by fixture patches |
| `rv32` | 4.0.5 | `bsp/gdr-rv32` supplied by fixture patches |

Patch series live under `patches/<target>/<version>/`. A patch must apply
cleanly to its own BSP and toolchain; do not share patches between the
Cortex-A9 and RV64 directories without verifying both. A9 sets
`RT_NAME_MAX=16` to match canonical fixture names. Shared coverage and matrix
policy are documented in [testing](../../docs/testing.md).
RT-Thread 3.1.4 layers its messagequeue sender-wait fixture onto the shared
3.1.x fixture; 3.1.0–3.1.3 have no sender-wait API.

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

## AN385 Armv7-M port fixture

```bash
python3 ci/test.py run rtthread mps2-an385 4.0.5
python3 ci/test.py matrix rtthread-ports
```

`patches/mps2-an385/4.0.5/` adds the BSP and canonical worker/IPC application to
the `v4.0.5` checkout. It compiles the unmodified upstream
`libcpu/arm/cortex-m3/context_gcc.S` and `cpuport.c` through the kernel's SCons
build. The fixture has no external SDK, FPU, TrustZone or SMP.
Compiler flags include `-O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer`.
The standard toolchain supplies newlib/nosys and `libgcc`, including the EHABI
personality routines referenced by the unwind tables.

QEMU boots `rtthread.elf` on `mps2-an385` without semihosting. The CMSDK UART0
at `0x40004000` supplies serial readiness output; SysTick uses the board's
25 MHz clock at 100 ticks/second. The linker maps 4 MiB flash at `0x0` and
4 MiB RAM at `0x20000000`, reserves a 64 KiB system heap and a non-overlapping
16 KiB MSP section, and exports `__StackLimit`/`__StackTop`. The vector table
contains 32 entries, including the real PendSV and SysTick handlers.

`gdr_unstarted_thread` remains initialized but is not started. The standard
worker threads and IPC objects retain the existing suite's names and waiter
relationships. Acceptance coverage lives in
[testing](../../docs/testing.md#rt-thread-suite-organization-and-capability-coverage);
runtime backtrace limits live in [errata](../../docs/errata.md#task-backtraces).

## microbit Armv6-M port fixture

```bash
python3 ci/test.py run rtthread microbit 4.0.5
```

`patches/microbit/4.0.5/` supplies the BSP and scopes the port compatibility
patch to its private build checkout. PendSV constructs `0xFFFFFFFD` for a
Thread/PSP return. The initial hardware-frame PC is halfword-aligned; the
normal task-exit LR retains its Thumb state bit. The saved integer context
keeps the upstream 64-byte layout. No CFI directives are added to libcpu.

The nRF51 TIMER2 at `0x4000A000` supplies 100 ticks/second through IRQ10
(exception 26). Its 16 MHz source is divided by prescaler 4 to 1 MHz;
CC0 is 10000 and the compare-to-clear shortcut resets the counter in hardware.
The SysTick vector is unused and the BSP does not access SysTick registers.
UART0 supplies serial output. QEMU SRAM is explicitly expanded to 256 KiB;
the linker reserves a 64 KiB heap and a disjoint 16 KiB MSP span.

Raw compatibility checks live in
[testing](../../docs/testing.md#rt-thread-suite-organization-and-capability-coverage).

## AN505 Armv8-M Mainline port fixture

```bash
python3 ci/test.py run rtthread mps2-an505 4.0.5
```

`patches/mps2-an505/4.0.5/` compiles upstream `libcpu/arm/cortex-m33/` with
`-mcpu=cortex-m33 -mthumb -mfloat-abi=soft`, `-O0 -g3 -gdwarf-4 -funwind-tables`
and frame pointers. The compatibility patch sets the assembly CPU to Cortex-M33,
clears the initial hardware PC's Thumb bit and uses `0xFFFFFFFD` for the first
Thread/PSP return in the fixture's single Secure state. No saved slots or CFI
directives are added to the port. `ARM_CM33_ENABLE_TRUSTZONE` is absent, so the
upstream security-context calls compile as stubs.

The image occupies the `0x10000000` Secure SSRAM alias; data occupies 2 MiB
at `0x28000000`. The linker reserves a 64 KiB heap and a disjoint 16 KiB MSP
section. The CMSDK UART0 Secure alias at `0x50200000` supplies readiness output;
SysTick uses a 20 MHz clock at 100 ticks/second. The canonical workers include
an initialized, unstarted thread and a three-function live chain. Acceptance
and runtime limits live in [testing](../../docs/testing.md#rt-thread-suite-organization-and-capability-coverage)
and [errata](../../docs/errata.md#task-backtraces).

## RV32 common integer port fixture

```bash
python3 ci/test.py run rtthread rv32 4.0.5
```

`patches/rv32/4.0.5/` selects upstream `libcpu/risc-v/common/cpuport.c` and
`context_gcc.S`, plus the E310 trap entry, without modifying libcpu. Compiler
flags include `-march=rv32imac_zicsr -mabi=ilp32 -mcmodel=medany`,
`-O0 -g3 -gdwarf-4 -funwind-tables -fno-omit-frame-pointer` and
`-msmall-data-limit=0`. Static thread stacks and kernel allocation are
16-byte aligned. Queue filling uses the kernel's initialized `max_msgs`, which
includes the configured message alignment.

QEMU boots the raw BIN at `0x80000000`; GDB loads the paired ELF. The BSP uses
the 16550 UART at `0x10000000` and the 10 MHz CLINT at `0x02000000`.
RV32 `mtime` reads use a high/low/high consistency check, and `mtimecmp` writes
use low-all-ones/high/low ordering. Timer traps drive 100 ticks/second; the
linker reserves a 64 KiB heap and a 16 KiB interrupt stack. The fixture exercises
the canonical IPC workers, an unstarted thread and a live three-function chain.
Trap unwinding scope lives in [errata](../../docs/errata.md#task-backtraces).
