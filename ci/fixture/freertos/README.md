# FreeRTOS fixture builds

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

Use the [shared CLI](../../README.md) for cache setup, tools, firmware
preparation and test execution. This document owns FreeRTOS-specific
compilation and measured platform facts; coverage and matrix coordinates
are described in [testing](../../../docs/testing.md).

| Variant family | Internal builder | Inputs |
| --- | --- | --- |
| Live kernel-direct | `build.sh` | Clean `FreeRTOS-Kernel` tag, shared application, board and config headers. |
| File-only `snapshot` | `build.sh` | Kernel headers and `profiles/config/snapshot/` data-only sources. |
| Isolated `eh-frame` | `../../bare/eh-frame/build.sh` | GNU assembly and linker script; no kernel checkout. |
| M-profile IRQ / sched / fault fixtures | `build.sh` | Board-specific `irq.c`, `sched.c` and `fault.c` under `profiles/board/<target>/` (`microbit`, `mps2-an385`, `mps2-an386`, `mps2-an505`) plus linker-reserved `.irq_stack`, all linked into `full` (and `fpu` on `mps2-an386`) under explicit runtime gate symbols. |

The shared runner supplies explicit source, build, compiler and install
paths. `build.sh` rejects a modified or mismatched tag checkout and never
changes it. Artifacts are published only after a complete successful build.
The compiler family follows the board (`arm-none-eabi-*` or
`riscv-none-elf-*`); the GDB must have Python and the matching target ISA.
The shared CLI's `doctor` command probes the selected tools before source
preparation.

## BSP and patch selection

| Target | ISA | Releases | Variants | Port / board source | Unwind coverage |
| --- | --- | --- | --- | --- | --- |
| `microbit` | Armv6-M | `11.1.0` | `full` | `portable/GCC/ARM_CM0`, `profiles/board/microbit/` | `sched` (PendSV/SVC stages), `irq` (single, nested, 4B `STKALIGN`), `fault` (`fault-frame-imprecise` on M0 `udf`), saved/live Thumb-1 + EHABI |
| `mps2-an385` | Armv7-M | `10.3.1`, `10.4.6`, `10.5.1`, `11.1.0` | `10.3.1`: config matrix (`base`, `full`, `tick16`, heap/static/stream); `10.4.6–10.5.1`: `base`; `11.1.0`: `base`, `full`, `tick16`, `streams` | `portable/GCC/ARM_CM3`, `profiles/board/mps2-an385/` | `sched` (cooperative/tick/nested-IRQ PendSV & `vTaskSwitchContext`), `irq` (single, nested, 4B `STKALIGN`, SysTick `xTaskIncrementTick`), `fault` (`UNDEFINSTR`, precise `BusFault`, `MemManage` `DACCVIOL`), EHABI `.ARM.exidx` |
| `mps2-an386` | Armv7E-M | `11.3.1` | `fpu` | `portable/GCC/ARM_CM4F`, `profiles/board/mps2-an386/` | `sched` (CM4F basic/FPU stages), `irq` (single, nested, 4B `STKALIGN`, FP `FPCA` lazy stacking, SysTick), `fault` (`UNDEFINSTR`, precise `BusFault`, `MemManage` `DACCVIOL`) |
| `mps2-an505` | Armv8-M Mainline | `11.3.1` | `full` | `portable/GCC/ARM_CM33_NTZ/non_secure`, `profiles/board/mps2-an505/` | `sched` (single Secure state CM33 stages), `irq` (single, nested, 4B `STKALIGN`, SysTick), `fault` (`UNDEFINSTR`, precise `BusFault`; MemManage N/A on QEMU AN505); TZ dual-world unsupported |
| `mps2-an521` | Armv8-M Mainline | `11.3.1`, `11.1.0` | `11.3.1`: `smp`, `mpu`; `11.1.0`: `snapshot` | `portable/GCC/ARM_CM33_NTZ/non_secure`, `profiles/board/mps2-an521/` | Saved/live task unwind on `smp` (with `smp-no-context` cross-core refusal) and `mpu` (`mpu-pool`); `snapshot` file-only negative ELF; SMP/MPU handler cross-stack unsupported |
| — | Armv8-M Baseline | — | — | — | No QEMU Cortex-M23 CPU/machine model; verified in unit tests only |
| `qemu-virt-rv32` | RV32 | `11.1.0` | `base` | `portable/GCC/RISC-V`, `profiles/board/qemu-virt-rv32/` | Saved/live CFI (`ilp32`), M-mode `freertos_risc_v_trap_handler` -> `xTaskIncrementTick` -> task cross-stack (`vTaskSwitchContext` fails closed); no vendor IRQ/fault extensions (XuanTie, Nuclei ECLIC, WCH PFIC) |
| `qemu-virt-rv64` | RV64 | `11.1.0` | `rv64` | `portable/GCC/RISC-V`, `profiles/board/qemu-virt-rv64/` | Saved/live CFI (`lp64`, 64-bit tick), M-mode `freertos_risc_v_trap_handler` cross-stack (`vTaskSwitchContext` fails closed); no vendor IRQ/fault extensions |

## Container tools and cache

For container runs, `ci/.cache/tools/linux-*` holds registered tool links,
not a self-contained installation of the Linux tools. The links resolve
inside the running container, for example:

```text
arm/                -> /opt/xpack-arm-none-eabi-gcc-<version>/bin
riscv/              -> /opt/xpack-riscv-none-elf-gcc-<version>/bin
bin/qemu-system-arm -> /usr/bin/qemu-system-arm
bin/gdb-multiarch   -> /usr/bin/gdb-multiarch
```

`setup` registers installed tools; it does not download or copy them.
`ci/containers/Dockerfile` and `ci/containers/Dockerfile.gdb12` must retain
their tool installation steps: without the matching image contents, the
links have no targets and tool preflight fails. The GDB 12 profile resolves
`/usr/bin/gdb-multiarch` in its own image, not in the default QEMU image.
Tool downloads occur when the relevant image build steps execute; repeated
test runs reuse installed tools through the image, not through the links.
See the shared [container instructions](../../README.md#containers) for image
reuse and `--no-build`.

Kernel source checkouts are separate cached data, not links to image
contents. Their reuse and validation follow the shared
[cache rules](../../README.md#cache-and-provenance).

## `microbit` (Armv6-M)

```bash
python3 ci/test.py run freertos microbit 11.1.0 full
```

### RTOS config

Links `portable/GCC/ARM_CM0` plus `portasm.c` and `profiles/board/microbit/`
(`configCPU_CLOCK_HZ=16000000`, `configPRIO_BITS=8`, `configENABLE_MPU=0`,
CLZ priority selection disabled for Thumb-1). Flash is 256 KiB at `0x0` and
RAM is expanded to 256 KiB at `0x20000000` (a fixture setting so `full` fits,
not physical micro:bit SRAM capacity). The top 4 KiB of RAM is `.irq_stack`
(`__gdr_irq_stack_low=0x2003F000`, `__gdr_irq_stack_top=0x20040000`, protected
by overlap assertions against `.bss` and heap). `g_pfnVectors` starts at `0`,
`__gdr_vector_table_end` is `192`, and `__gdr_armv6m_irq_count` is `32` (16
system + 32 external vectors). nRF51 TIMER0 (`0x40008000`, IRQ8, priority `0xC0`)
and TIMER1 (`0x40009000`, IRQ9, priority `0x40`) drive external interrupts.

### Build option

Built with `arm-none-eabi-*`, `-mcpu=cortex-m0 -mthumb -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables -Wall -Wextra -Werror -std=c11`, `-Wl,--gc-sections -Wl,--undefined=gdr_freertos_version_num`, and `-nostartfiles`. Links `board/microbit/irq.c`, `sched.c`, and `fault.c` into `full`.

### QEMU boot

`qemu-system-arm -M microbit -global nrf51-soc.sram-size=262144 -semihosting-config enable=on,target=native -kernel freertos.elf`;
GDB uses `arm` and waits for semihosting `"GDR FreeRTOS fixture ready."`.

### Unwind testcase

- **Scheduled & saved**: Verifies Cortex-M0 saved-context slot layout (`EXC_RETURN` at top, PC at +60, xPSR at +64), Thumb-1 C CFI chains, and `gdr_sched_gate`-controlled PendSV/SVC ownership across `ARM_CM0`.
- **IRQ (`gdr_irq_gate`)**: `gdr_irq_task` runs `GDR_IRQ_SCENARIO_SINGLE`, `GDR_IRQ_SCENARIO_NESTED` (TIMER1 preempting TIMER0 across MSP), and `GDR_IRQ_SCENARIO_ALIGN` (4-byte SP misalignment exercising `STKALIGN` padding).
- **Fault (`gdr_fault_gate`)**: `gdr_fault_trigger` executes `udf #0` (`gdr_fault_udf_leaf`); M0 lacks fault status registers (`CFSR`), so GDR stops cleanly with `fault-frame-imprecise` while the handler advances stacked PC to keep the session healthy. Acceptance and limits live in [testing](../../../docs/testing.md#exception-unwind-acceptance) and [errata](../../../docs/errata.md#task-backtraces).

## `mps2-an385` (Armv7-M)

```bash
python3 ci/test.py run freertos mps2-an385 10.3.1 base
python3 ci/test.py run freertos mps2-an385 10.3.1 full
python3 ci/test.py run freertos mps2-an385 11.1.0 full
```

### RTOS config

Links `portable/GCC/ARM_CM3` and `profiles/board/mps2-an385/`
(`configCPU_CLOCK_HZ=25000000`, 4 MiB flash at `0x0`, 4 MiB RAM at `0x20000000`,
top 16 KiB `.irq_stack` exporting `__gdr_irq_stack_low=0x203FC000` and
`__gdr_irq_stack_top=0x20400000`, equal to `_estack`). CMSDK Timer0 (`0x40000000`,
IRQ8, priority `0xFF`) and Timer1 (`0x40001000`, IRQ9, priority `0x40`) drive
external interrupt and scheduler IRQ tests.

Measured NVIC requirement: QEMU's `cortex-m3` model implements all 8 priority
bits (`0xFF` reads back `0xFF`). V10.4/V10.5 `ARM_CM3/port.c` asserts probed
priority bits equal `configPRIO_BITS`, so the board sets `configPRIO_BITS=8`.
V11.1.0 requires even syscall priority (sub-priority guard), so
`configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY=4`.

### Build option

Built with `arm-none-eabi-*`, `-mcpu=cortex-m3 -mthumb -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables -Wall -Wextra -Werror -std=c11`.
Links `irq.c`, `sched.c`, and `fault.c` into `full`. With `--no-merge-exidx-entries`,
the linker preserves distinct `.ARM.exidx` entries for adjacent kernel functions
with identical unwind opcodes so EHABI coverage can be asserted directly against
compiler tables.

Carries both the `10.3.1` (`V10.3.1-kernel-only`) config matrix and the
`10.4.6` / `10.5.1` / `11.1.0` version sweep (`base`, `full`, `tick16`, `streams`).

### QEMU boot

`qemu-system-arm -M mps2-an385 -semihosting-config enable=on,target=native -kernel freertos.elf`;
GDB uses `arm` and waits for `"GDR FreeRTOS fixture ready."`.

### Unwind testcase

- **Scheduled (`gdr_sched_gate`)**: `gdr_sched_gate` selects cooperative yield, tick preemption (`vTaskSwitchContext`), and nested CMSDK Timer0/Timer1 IRQ scheduling; `gdr_sched_first` tests the initial SVC restore path on a reset session.
- **IRQ (`gdr_irq_gate`)**: `gdr_irq` cycles `GDR_IRQ_SCENARIO_SINGLE`, `GDR_IRQ_SCENARIO_NESTED`, `GDR_IRQ_SCENARIO_ALIGN`, C callee chains inside handlers, and SysTick (`xTaskIncrementTick`) cross-stack unwinding across MSP/PSP.
- **Fault (`gdr_fault_gate`)**: `gdr_fault` cycles `GDR_FAULT_SCENARIO_UNDEFINSTR` (`FORCED` + `UFSR.UNDEFINSTR` escalated to HardFault), `GDR_FAULT_SCENARIO_BUS` (`0x50000000` precise BusFault `PRECISERR` + `BFARVALID`), and `GDR_FAULT_SCENARIO_MEMMANAGE` (MPU-blocked load `DACCVIOL` + `MMARVALID` + `MMFAR`) at global `gdr_fault_*_stop` labels. Each handler advances stacked PC (MemManage resumes at stacked LR) so the halted session remains healthy; `IACCVIOL` stays unit-only because branching into an execute-never region does not raise a recoverable direct fault on QEMU MPS2.

## `mps2-an386` (Armv7E-M)

```bash
python3 ci/test.py run freertos mps2-an386 11.3.1 fpu
```

### RTOS config

Links `V11.3.1` `portable/GCC/ARM_CM4F` with `profiles/board/mps2-an386/`
(single Cortex-M4 with FPU: `MVFR0` nonzero, `CPACR` writable; `configCPU_CLOCK_HZ=25000000`,
`configPRIO_BITS=8`, 4 MiB flash at `0x0`, 4 MiB RAM at `0x20000000`, top 16 KiB
`.irq_stack`, CMSDK Timer0/1). Variant `fpu` enables `configENABLE_FPU 1`, links
`irq.c`, `sched.c`, and `fault.c`, and adds `gdr_fpu_task` executing VFP
instructions before blocking; AN505/AN521 models lack FPU hardware so FPU
coverage lives here.

### Build option

Built with `arm-none-eabi-*`, `-mcpu=cortex-m4 -mthumb -mfloat-abi=hard -mfpu=fpv4-sp-d16 -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables -Wall -Wextra -Werror -std=c11`.

### QEMU boot

`qemu-system-arm -M mps2-an386 -semihosting-config enable=on,target=native -kernel freertos.elf`;
GDB uses `arm`.

### Unwind testcase

- **Scheduled & FPU (`gdr_sched_gate`)**: Verifies CM4F basic/FPU switch profiles alongside extended saved FPU frames (`s16-s31` / `EXC_RETURN`).
- **IRQ (`gdr_irq_gate`)**: `gdr_irq` runs `GDR_IRQ_SCENARIO_SINGLE`, `GDR_IRQ_SCENARIO_NESTED`, `GDR_IRQ_SCENARIO_ALIGN`, and `GDR_IRQ_SCENARIO_FP` (`FPCA` lazy stacking across MSP).
- **Fault (`gdr_fault_gate`)**: `gdr_fault` cycles `GDR_FAULT_SCENARIO_UNDEFINSTR`, `GDR_FAULT_SCENARIO_BUS`, and `GDR_FAULT_SCENARIO_MEMMANAGE` (`DACCVIOL`), exercising both precise HardFault escalation and direct MemManage cross-stack unwinding.

## `mps2-an505` (Armv8-M Mainline)

```bash
python3 ci/test.py run freertos mps2-an505 11.3.1 full
```

### RTOS config

Links `V11.3.1` `portable/GCC/ARM_CM33_NTZ/non_secure` single-core Secure-only
(`configENABLE_TRUSTZONE=0`, `configENABLE_MPU=0`, `configENABLE_FPU=0`,
`configRUN_FREERTOS_SECURE_ONLY=1`) with `profiles/board/mps2-an505/`
(`configCPU_CLOCK_HZ=20000000`, `configPRIO_BITS=8`). Maps 4 MiB flash at
`0x10000000` (Secure SSRAM alias), 2 MiB RAM at `0x28000000`, top 16 KiB
`.irq_stack`, and Secure APB Timer0 (`0x50000000`, IRQ3 / exception 19) and
Timer1 (`0x50001000`, IRQ4 / exception 20).

### Build option

Built with `arm-none-eabi-*`, `-mcpu=cortex-m33 -mthumb -mfloat-abi=soft -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables -Wall -Wextra -Werror -std=c11`.
Links `irq.c`, `sched.c`, and `fault.c` into `full`.

### QEMU boot

`qemu-system-arm -M mps2-an505 -semihosting-config enable=on,target=native -kernel freertos.elf`;
GDB uses `arm`.

### Unwind testcase

- **Scheduled (`gdr_sched_gate`)**: `ARM_CM33` PendSV/SVC transition stages and `vTaskSwitchContext` ownership in single Secure state (dual-world TZ switching is unsupported).
- **IRQ (`gdr_irq_gate`)**: `GDR_IRQ_SCENARIO_SINGLE`, `GDR_IRQ_SCENARIO_NESTED`, `GDR_IRQ_SCENARIO_ALIGN`, and SysTick cross-stack unwind using exceptions 19/20.
- **Fault (`gdr_fault_gate`)**: `GDR_FAULT_SCENARIO_UNDEFINSTR` and `GDR_FAULT_SCENARIO_BUS` (`0xA0000000` precise BusFault); MemManage `DACCVIOL` is omitted on AN505 (verified on AN385/AN386).

## `mps2-an521` (Armv8-M Mainline)

```bash
python3 ci/test.py run freertos mps2-an521 11.3.1 smp
python3 ci/test.py run freertos mps2-an521 11.3.1 mpu
python3 ci/test.py run freertos mps2-an521 11.1.0 snapshot
```

### RTOS config

Links `portable/GCC/ARM_CM33_NTZ/non_secure` with `profiles/board/mps2-an521/`
(SSE-200 dual Cortex-M33, 20 MHz `configCPU_CLOCK_HZ`, `configPRIO_BITS=8`,
4 MiB Secure SSRAM alias at `0x10000000`, 2 MiB RAM at `0x28000000`).

- **`smp` (`V11.3.1`)**: Requires `V11.3.1+` (`portVALIDATED_FOR_SMP == 1`; earlier tags lack `configCORE_ID_REGISTER` / `configWAKE_SECONDARY_CORES`). `config/smp/FreeRTOSConfig.h` sets `configNUMBER_OF_CORES=2`, `configRUN_MULTIPLE_PRIORITIES`, `configUSE_PASSIVE_IDLE_HOOK`, `configUSE_PORT_OPTIMISED_TASK_SELECTION=0`, `configCORE_ID_REGISTER` pointing to `0x5001F000` (reads `0` on CPU0, `1` on CPU1; `SCB->CPUID` reads identically on both cores), and `configWAKE_SECONDARY_CORES` clearing `CPUWAIT` bit 1 after setting `INITSVTOR1`. Because the port's `vInterruptCore` is a weak no-op without an overridden MHU doorbell (`0x40003000`/`0x40004000`), shared waiter tasks pin to core 0 while `gdr_bound` and passive idle keep core 1 active.
- **`mpu` (`V11.3.1`)**: Single-core (`configNUMBER_OF_CORES=1`, `configENABLE_MPU=1`, `configRUN_FREERTOS_SECURE_ONLY=1`, `configENABLE_TRUSTZONE=0`; SMP + MPU triggers compile `#error`). Links `portable/Common/mpu_wrappers_v2.c` and `mpu_wrappers_v2_asm.c`, registering created objects in `xKernelObjectPool`.
- **`snapshot` (`V11.1.0`)**: Offline Cortex-M33 SMP ELF pair (`snapshot.elf`, `snapshot_heap.elf`); never booted on QEMU.

### Build option

Built with `arm-none-eabi-*`, `-mcpu=cortex-m33 -mthumb -mfloat-abi=soft -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables`
(adding `-Wno-unused-parameter` for upstream `mpu_wrappers_v2*`). Shared `board/mps2-an521/linker.ld` satisfies two PMSAv8 requirements: it collects un-dotted `*(privileged_functions*)`, `*(freertos_system_calls*)`, and `*(privileged_data*)` sections (non-empty bounds required by `port.c`'s SVC check) and places the three flash windows in disjoint subregions with inclusive `RLAR` end bounds (`__*_end__ = start + size - 1`) to avoid `UFSR.INVPC` in `prvSetupMPU`.

### QEMU boot

`qemu-system-arm -M mps2-an521 -semihosting-config enable=on,target=native -kernel freertos.elf`
(uses machine default two CPUs; do not pass `-smp 1`). `snapshot` loads ELF files directly via GDB `file` without QEMU.

### Unwind testcase

Verifies dual-core SMP task discovery (`CPU`, `Affinity`, `RunState`, `PreemptionDisable`), `smp-no-context` refusal on running secondary-core contexts without register inferiors, saved/live task backtrace on `smp` and `mpu`, and `mpu-pool` (`xKernelObjectPool`) object discovery. SMP and MPU handler cross-stack unwinding remains unsupported.

## `qemu-virt-rv32` (RV32)

```bash
python3 ci/test.py run freertos qemu-virt-rv32 11.1.0 base
```

### RTOS config

Links `V11.1.0` `portable/GCC/RISC-V` with `profiles/board/qemu-virt-rv32/`
(QEMU `virt` RAM: 128 MiB at `0x80000000`, no flash, 10 MHz CLINT timer `configCPU_CLOCK_HZ=10000000`, `configISR_STACK_SIZE_WORDS=512`). Retains `.eh_frame` with `KEEP` in `linker.ld` so `--gc-sections` does not discard unwind tables.

### Build option

Built with `riscv-none-elf-*`, `-march=rv32imac_zicsr -mabi=ilp32 -mcmodel=medany -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables --specs=nano.specs --specs=nosys.specs`, producing `freertos.elf` and `freertos.bin`.

### QEMU boot

`qemu-system-riscv32 -M virt -cpu rv32 -m 256M -semihosting-config enable=on -bios freertos.bin`;
GDB loads `freertos.elf` (`riscv:rv32`).

### Unwind testcase

Covers 32-bit pointer width (`ilp32`), saved-context and live C CFI chains (matching RV64's first saved PC slot layout with 4-byte words), and M-mode `freertos_risc_v_trap_handler` -> `xTaskIncrementTick` -> multi-frame task cross-stack unwinding (`vTaskSwitchContext` stops fail closed). Vendor RISC-V IRQ/fault extensions (XuanTie, Nuclei ECLIC, WCH PFIC) are unsupported.

## `qemu-virt-rv64` (RV64)

```bash
python3 ci/test.py run freertos qemu-virt-rv64 11.1.0 rv64
```

### RTOS config

Links `V11.1.0` `portable/GCC/RISC-V` with `profiles/board/qemu-virt-rv64/`
(128 MiB RAM at `0x80000000`, 10 MHz CLINT timer, `configISR_STACK_SIZE_WORDS=512`).
Requires variant `rv64`: the RISC-V port hardcodes `TickType_t` to 64 bits on
RV64, whereas plain `base` leaves `configTICK_TYPE_WIDTH_IN_BITS` at 32 and
misplaces event-group control bits; `config/rv64/FreeRTOSConfig.h` sets
`TICK_TYPE_WIDTH_64_BITS`. Retains `.eh_frame` with `KEEP` in `linker.ld`.

### Build option

Built with `riscv-none-elf-*`, `-march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany -Og -g3 -ffunction-sections -fdata-sections -fno-lto -funwind-tables --specs=nano.specs --specs=nosys.specs`, producing `freertos.elf` and `freertos.bin`.

### QEMU boot

`qemu-system-riscv64 -M virt -cpu rv64 -m 256M -semihosting-config enable=on -bios freertos.bin`;
GDB loads `freertos.elf` (`riscv:rv64`).

### Unwind testcase

Covers 64-bit pointer/tick width (`lp64`), non-first task exit sentinel (`0`),
live CFI chains, and M-mode `freertos_risc_v_trap_handler` -> `xTaskIncrementTick` ->
multi-frame task cross-stack unwinding (`vTaskSwitchContext` fails closed).
Vendor RISC-V IRQ/fault extensions are unsupported.

## Shared config variants

- **16-bit tick (`tick16`)**: `config/tick16/FreeRTOSConfig.h` defines
  `configUSE_16_BIT_TICKS 1` before `gdr_fixture_common.h` (`configTICK_TYPE_WIDTH_IN_BITS`
  exists only from V10.6.0, and `FreeRTOS.h` rejects configs defining both).
  `sizeof(TickType_t) == 2` and `portMAX_DELAY == 0xffff` at 1000 Hz (~65.5 s
  wrap, so assertions never hardcode absolute ticks); exercises tick-width
  event-group masks, sentinels, and list-value reads across `10.3.1` and `11.1.0`.
- **Cross-version ABI deltas (`mps2-an385` `10.3.1`–`11.1.0`)**: Notification
  fields become arrays in `V10.4.0`; `configUSE_MINI_LIST_ITEM` arrives in
  `V10.5.0`; `V11.0.0` removes `xBlockAllocatedBit`, adds `configENABLE_HEAP_PROTECTOR` /
  `xHeapCanary`, and turns `ulTotalRunTime` into an array in every build.
  `configENABLE_HEAP_PROTECTOR` links `vApplicationGetRandomHeapCanary` returning
  non-zero `0xBEEF` so heap XOR obfuscation is provably active.

## Static snapshot variant

`build.sh --variant snapshot` compiles `profiles/config/snapshot/snapshot.c` for
Cortex-M33 into `snapshot.elf` and pairs it with `snapshot_heap.elf`
(`snapshot_heap.c`) on cell `mps2-an521/11.1.0/snapshot`.
`tests/integration/freertos/test_snapshot.py` loads each ELF with GDB `file`
only (no QEMU, no `target remote`), keeping 4-byte pointers and target ABI types.
A separate `snapshot_heap.elf` isolates the free-list-member-with-allocated-bit
corruption because `cross_validate` refuses to compare metrics across one
corrupt walk.

Measured file-only GDB rules shaping the snapshot sources:

1. `.data` returns programmed bytes to both `read_memory()` and `parse_and_eval`,
   whereas unallocated `.bss` synthesises zeros instead of raising `MemoryError`;
   every decoded snapshot global carries a non-zero initialiser so it lands in
   `.data`.
2. Snapshot TCBs mirror V11.0.0+ SMP (`configNUMBER_OF_CORES=2`, pinned to
   `11.1.0`) with stack-fill words `0xa5a5a5a5` (matching byte-wise stack fill
   for high-water scans). Because `struct tskTaskControlBlock` enables all SMP
   and stats fields unconditionally, field-gating probes remain validated on
   live config variants.

## Variants that no fixture can reach

- **Upward-growing stacks** (`portSTACK_GROWTH +1`) exist only in
  `portable/SDCC/Cygnal`. No GCC port and no QEMU machine can host it; GDR
  treats stacks as grow-down only (`stack_grows_up` is absent).
