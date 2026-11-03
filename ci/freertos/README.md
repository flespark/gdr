# FreeRTOS fixture builds

Use the [shared CLI](../README.md) for cache setup, tools, firmware
preparation and test execution. This document owns FreeRTOS-specific
compilation and measured platform facts; coverage and matrix coordinates
are described in [testing](../../docs/testing.md).

| Variant family | Internal builder | Inputs |
| --- | --- | --- |
| Live kernel-direct | `build.sh` | Clean `FreeRTOS-Kernel` tag, shared application, board and config headers. |
| File-only `snapshot` | `build.sh` | Kernel headers and `fixture/config/snapshot/` data-only sources. |
| Isolated `eh-frame` | `fixture/eh-frame/build.sh` | GNU assembly and linker script; no kernel checkout. |
| M0/M3 IRQ full fixtures | `build.sh` | Board-specific `irq.c` under `microbit/` or `mps2-an385/`, full config and linker-reserved MSP stack; other boards/variants omit the IRQ task. |
| AN385 scheduler fixture | `build.sh` | `fixture/board/mps2-an385/sched.c`, `config/sched/` and the linker-reserved MSP stack; excludes the full fixture's IRQ task. |

The shared runner supplies explicit source, build, compiler and install
paths. `build.sh` rejects a modified or mismatched tag checkout and never
changes it. Artifacts are published only after a complete successful build.
The compiler family follows the board (`arm-none-eabi-*` or
`riscv-none-elf-*`); the GDB must have Python and the matching target ISA.
The shared CLI's `doctor` command probes the selected tools before source
preparation.

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
See the shared [container instructions](../README.md#containers) for image
reuse and `--no-build`.

Kernel source checkouts are separate cached data, not links to image
contents. Their reuse and validation follow the shared
[cache rules](../README.md#cache-and-provenance).

## Live lanes

### Cortex-M0 (`microbit`, variant `full`)

`build.sh --target microbit --variant full` links the shared
fixture against `portable/GCC/ARM_CM0` plus `portasm.c` and the micro:bit
startup/linker (256 KiB flash). QEMU uses `-M microbit` with
`-global nrf51-soc.sram-size=262144`, matching the linker's 256 KiB SRAM
region so the shared `full` configuration fits. This expanded SRAM is a
test-fixture setting, not the physical board's capacity. `configENABLE_MPU`
is 0 and the CLZ-based priority selection is disabled for the Thumb-1 port.
GDB uses `arm` and the same semihosting ready marker. This lane verifies the Cortex-M0 saved-context
offset (EXC_RETURN at the top, PC at +60, xPSR at +64) and the Thumb-1
backtrace chain.

The 11.1.0 `full` fixture also compiles `board/microbit/irq.c`. The linker
reserves the top 4 KiB as `.irq_stack`, exporting `__gdr_irq_stack_low`
(`0x2003F000`) and `__gdr_irq_stack_top` (`0x20040000`); overlap assertions
protect `.bss` and heap storage. `g_pfnVectors` is zero,
`__gdr_vector_table_end` is 192, and absolute symbol
`__gdr_armv6m_irq_count` is 32. The vector table has 16 system and 32
external slots; only TIMER0/1 handlers are exercised by this IRQ fixture.

nRF51 TIMER0 (`0x40008000`, IRQ8, priority `0xC0`) and TIMER1
(`0x40009000`, IRQ9, priority `0x40`) generate real exceptions. TIMER1
preempts TIMER0; a separate task wait reduces SP by four bytes to exercise
hardware padding. Acceptance and support limits live in
[testing](../../docs/testing.md#exception-unwind-acceptance) and
[errata](../../docs/errata.md#task-backtraces).

### Kernel-direct (`mps2-an385`)

`build.sh` reads the prepared kernel tag and links it with
`portable/GCC/ARM_CM3` plus `fixture/board/mps2-an385/`. QEMU machine is
`qemu-system-arm -M mps2-an385`. Source checkouts are immutable; only the
private build directory receives compiler output. The 11.1.0 `full` link
retains separate `.ARM.exidx` entries for adjacent kernel functions with
identical unwind opcodes, so the live backtrace fixture can assert EHABI
coverage directly.

This lane carries both matrices: the config variants at 10.3.1 (the
`V10.3.1-kernel-only` tag) and the version sweep. The tags matter because they
bracket ABI
changes: notification fields became arrays in V10.4.0, `configUSE_MINI_LIST_ITEM`
arrived in V10.5.0, and V11.0.0 removed `xBlockAllocatedBit`, added
`configENABLE_HEAP_PROTECTOR` / `xHeapCanary`, and made `ulTotalRunTime` an
array in *every* build including single-core ones.

Measured NVIC fact that gates this lane: QEMU's `cortex-m3` model implements
**all 8 priority bits** (writing 0xFF to an NVIC priority register reads back
0xFF). V10.4/V10.5 ARM_CM3 `port.c` asserts the probe-derived bit count equals
`configPRIO_BITS`, so the board header sets `configPRIO_BITS 8`; V11.1.0 then
requires the syscall priority to be even (sub-priority confusion guard), so
`configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY` is 4. Wrong values make every
V10 build die in `gdr_fixture_assert_failed` inside `xPortStartScheduler` with
no serial output - indistinguishable from "the version doesn't boot" until the
PC is read.

`configENABLE_HEAP_PROTECTOR` (V11 only) also requires the fixture to provide
`vApplicationGetRandomHeapCanary`; without it the build fails to link. The
fixture returns a fixed non-zero canary (0xBEEF) so tests can XOR heap link
fields back and prove the obfuscation is really active - a zero canary would
silently degrade to the unprotected layout.

### AN385 basic IRQ fixture (`full`)

`build.sh` compiles `fixture/board/mps2-an385/irq.c` only for AN385 `full`.
The linker reserves the top 16 KiB of RAM as `.irq_stack`, exporting
`__gdr_irq_stack_low` at `0x203FC000` and `__gdr_irq_stack_top` at
`0x20400000`; `_estack` equals the upper bound. Linker assertions reject
overlap with `.bss` or the heap.

The `gdr_irq` task drives CMSDK timer0 at `0x40000000` (IRQ8, priority
`0xFF`) and timer1 at `0x40001000` (IRQ9, priority `0x40`). Timer1 is armed
inside the timer0 handler to produce nesting. The fixture cycles single IRQ,
nested IRQ and four-byte SP misalignment to exercise STKALIGN padding;
handlers also make ordinary C calls. The live exception test scope is
FreeRTOS 10.3.1 (`V10.3.1-kernel-only`); see
[capability coverage](../../docs/testing.md#freertos-suite-organization-and-capability-coverage)
for the independent oracle and limits.

### AN385 scheduler fixture (`sched`)

`mps2-an385/10.3.1/sched` links `V10.3.1-kernel-only` with
`portable/GCC/ARM_CM3`, `fixture/config/sched/FreeRTOSConfig.h` and
`fixture/board/mps2-an385/sched.c`. The config retains stack-end recording,
runtime statistics and overflow checks. The full fixture's `irq.c` is absent
so its high-priority task cannot interfere with scheduler scenarios. The
MSP stack uses the board's linker-owned span described above.

`gdr_sched_gate` selects cooperative yield, tick preemption or nested CMSDK
timer IRQ scheduling. Separate task handles and scenario counters expose
the old/new task states; `gdr_sched_first` supplies the first-start SVC
scenario in a reset session. Gate writes and target execution are fixture
setup, not operations performed by GDR queries. Verification and oracle
scope live in [testing](../../docs/testing.md#freertos-suite-organization-and-capability-coverage).

### Kernel-direct SMP (`mps2-an521`)

`build.sh` with `--target mps2-an521` links the same shared
fixture against the **dual-core** ARMv8-M port `portable/GCC/ARM_CM33_NTZ/non_secure`
and the `mps2-an521` board (QEMU machine `mps2-an521`, two Cortex-M33s, SSE-200).
This lane needs kernel `V11.3.1` or later: ARMv8-M SMP support was added in
`V11.3.1` (History.txt), and the CM33_NTZ port is the only mainline GCC port
with `portVALIDATED_FOR_SMP == 1`. Earlier tags have no `configCORE_ID_REGISTER` /
`configWAKE_SECONDARY_CORES` and fail at `#error` under `configNUMBER_OF_CORES 2`.

The `config/smp/FreeRTOSConfig.h` variant sets two cores, core affinity, task
preemption disable, and `configENABLE_TRUSTZONE/MPU/FPU 0`; the four SMP-required
knobs (`configRUN_MULTIPLE_PRIORITIES`, `configUSE_PASSIVE_IDLE_HOOK`,
`configUSE_PORT_OPTIMISED_TASK_SELECTION 0`, and `configCORE_ID_REGISTER` +
`configWAKE_SECONDARY_CORES`) are all spelled out because omitting any of them
is a compile-time `#error` on this port. The board header fixes `configCPU_CLOCK_HZ`
to the AN521's 20 MHz sysclk (not the AN385's 25 MHz).

QEMU boots both cores from one vector table (`INITSVTOR1` default `0x10000000`
aliases the `0x00000000` image); `configWAKE_SECONDARY_CORES` writes
`INITSVTOR1` then clears `CPUWAIT` bit 1 to release CPU1's warm reset, and
CPU1's private entry runs the official secondary-core flow (spin on
`ucPrimaryCoreInitDoneFlag`, program interrupt priorities, set
`ucSecondaryCoresReadyFlags`, `svc 102`). The CPU identity register
`0x5001F000` (secure alias) reads 0 on CPU0 and 1 on CPU1 and is used for
`configCORE_ID_REGISTER`; the SCB `CPUID` is *not* usable because both cores
report the same value.

**Known fixture limitation (cross-core preemption).** The port's
`vInterruptCore` is a weak no-op and this board does not override it, so a
cross-core yield request is not delivered as an interrupt: a blocked task
caught mid-yield renders as `Running(yielding)` and its waiter count reads 0
until its own tick. This is a fixture limitation, not a GDR defect; the SMP
user-visible output (CPU / Affinity / RunState / PreemptionDisable) all render
from shared memory. main.c mitigates the shared single-core assertions by
pinning every ground-truth waiter task to core 0 under
`configNUMBER_OF_CORES > 1`, so no shared test ever needs a cross-core yield
(`gdr_bound` on core 1 and the two idle tasks keep the lane genuinely
dual-core). QEMU does model SSE-200 MHU doorbells, but they sit in the shared
container at `0x40003000`/`0x40004000` (the `0x5000_0000` secure alias maps
the per-CPU container, not the MHU block), and delivering a cross-core
interrupt into this `configRUN_FREERTOS_SECURE_ONLY 1` build was not validated
here; it is left for a future lane.

### Single-core MPU wrappers (`mps2-an521`, variant `mpu`)

The same board directory hosts a **single-core** `configENABLE_MPU 1` variant
(`config/smp/FreeRTOSConfig.h` sets two cores; `config/mpu/FreeRTOSConfig.h`
keeps `configNUMBER_OF_CORES 1`, because SMP and MPU are mutually exclusive on
ARMv8-M -- portmacrocommon.h `#error`s on the combination). The build links
`portable/Common/mpu_wrappers_v2.c` and the port's `mpu_wrappers_v2_asm.c`; the
headers macro-rewrite every plain create to its `MPU_*` counterpart, which
records the object in `xKernelObjectPool` -- the only complete kernel object
registry, and the `mpu-pool` discovery channel's live fixture.

The variant needs `configRUN_FREERTOS_SECURE_ONLY 1` + `configENABLE_TRUSTZONE 0`
(the port's secure-side-only combination, port.c) and **the board linker script
must lay the MPU regions out correctly**. Two joint requirements, both in
`board/mps2-an521/linker.ld`:

1. The kernel marks its privileged entry points with
   `__attribute__((section("privileged_functions")))` (mpu_wrappers.h -- the GCC
   section names have **no leading dot**), so the script must collect
   `*(privileged_functions*)`, `*(freertos_system_calls*)` and
   `*(privileged_data*)`. A dotted spelling matches nothing, silently leaves the
   real section as an orphan and produces
   `__privileged_functions_start__ == __privileged_functions_end__`, so
   `port.c`'s SVC gate (`ulPC >= start && ulPC <= end`) never admits
   `xPortStartScheduler` and the scheduler never starts.
2. ARMv8-M PMSAv8 forbids overlapping MPU regions: the three flash windows
   (privileged functions, system calls, unprivileged task code) must be
   disjoint subregions, not three aliases of the whole flash window -- an
   overlap faults with `UFSR.INVPC` inside `prvSetupMPU`. The region end
   symbols are the RLAR **inclusive** limit, so `__*_end__ = start + size - 1`.

The earlier "boots are unstable/unreachable under QEMU's SSE-200" conclusion
was wrong: it was this fixture linker script (both points at once). With the
regions fixed the variant boots on the same machine the `smp` fixture boots,
and the pool is populated at boot (36 of 48 slots with the current fixture
object set -- a figure that drifts when the fixture creates more objects, so
the live assertion only requires a non-empty pool). `smp` and `mpu` share the
one linker script, so a regression on either variant fails the same CI stage.

### 16-bit tick (`tick16`)

`config/tick16/FreeRTOSConfig.h` defines `configUSE_16_BIT_TICKS 1` **before**
`gdr_fixture_common.h` (the shared header's `#ifndef` guard would otherwise
force its own 0; the variant cannot use `configTICK_TYPE_WIDTH_IN_BITS`, which
only exists from V10.6.0, and FreeRTOS.h rejects a config defining both).
`sizeof(TickType_t) == 2` and `portMAX_DELAY` shrinks to `0xffff`, so this is
the live cell for the tick-width-derived event-group masks, the `portMAX_DELAY`
sentinels and the width-correct list-value raw reads on every supported lane
(the 10.3.1 and 11.1.0 kernel-direct cells). The tick wraps every
~65.5 s at 1000 Hz, so no assertion may hard-code absolute tick values.

### 32-bit RISC-V (`qemu-virt-rv32`, variant `base`)

`build.sh --target qemu-virt-rv32 --variant base` links
`portable/GCC/RISC-V` for `rv32imac_zicsr`/`ilp32`; QEMU machine is
`qemu-system-riscv32 -M virt -cpu rv32 -bios freertos.bin`, GDB uses
`riscv:rv32`.  The saved-context layout uses the same first PC slot as RV64
with 4-byte words, and the lane verifies 32-bit pointer width and the CFI
walk.

### 64-bit RISC-V (`qemu-virt-rv64`, variant `rv64`)

The lane must run the `rv64` variant: the RISC-V port hardcodes
`TickType_t` to the architecture width (64), while the kernel's tick-width
*constants* follow `configTICK_TYPE_WIDTH_IN_BITS`, which a plain variant
leaves at 32 — the event-group control bits then land at bits 24..31 on a
64-bit `TickType_t`, which the event decode and the `tick_bits` profile
comparison correctly flag. The `rv64` config declares the 64-bit width
(see its header for the full chain); `qemu-virt-rv64 11.1.0 base` is not a
valid cell.

## Static snapshot variant

`build.sh --variant snapshot` compiles
`fixture/config/snapshot/snapshot.c` for Cortex-M33 into an ELF that is never
executed — the negative-testing arm for data-corruption scenarios. The matrix
runner launches it as the ordinary `snapshot` variant on the cell
`mps2-an521/11.1.0`; `tests/integration/freertos/test_snapshot.py` then loads
the ELF with GDB's `file` command only, with no QEMU and no `target remote`.
It keeps 4-byte pointers and the real ABI types so DWARF matches a genuine
target. A second, heap-only ELF (`snapshot_heap.c`, installed alongside as
`snapshot_heap.elf`) carries the free-list-member-with-allocated-bit
corruption, because one heap symbol set can only show one corruption and
`cross_validate` refuses to compare numbers over a corrupt walk.

The snapshot carries the diagnostic negatives a healthy kernel cannot
produce: a timer on the overflow list, a heap whose free-list/linear/counter
triple disagrees, and corrupted scheduler lists (`gdr_bad_*`). The shared runner fingerprints snapshot inputs before tests; the test
module requires the prepared pair and never rebuilds it.

Two measured facts shape the design:

1. With only `file <elf>` loaded, `gdb.selected_inferior().read_memory()` and
   `parse_and_eval` both return the programmed bytes for `.data`.
2. The same read against `.bss` returns zeros instead of raising
   `MemoryError` - file-only GDB synthesises zeros for unallocated BSS.

So every snapshot global that GDR must decode carries a non-zero initialiser
and therefore lands in `.data`. A BSS-resident structure would read back as a
successful decode of empty lists and NULL pointers, which is indistinguishable
from a healthy but empty kernel.

The snapshot is an SMP image (`configNUMBER_OF_CORES 2`), which the kernel only
supports from V11.0.0 onwards, so its cell pins kernel 11.1.0.
Stack fill words use `0xa5a5a5a5`, not `0xa5`: the kernel's fill byte is applied
per byte, so an untouched `StackType_t` word reads as the repeated pattern. A
word holding plain `0xa5` would stop the high-water scan after one byte and
report zero free bytes - a fake friendlier than the real thing, which would hide
watermark regressions.

The snapshot's TCB is a hand-written struct whose tag matches `tasks.c`
(`struct tskTaskControlBlock`) with every SMP and statistics member present.
That is deliberate for decoding coverage, but it also means this lane cannot
falsify field *gating*: the stand-in is friendlier than a real kernel, where a
member's existence depends on the config. Gating must be proved on a live lane.

## Variants that no fixture can reach

- **Upward-growing stacks** (`portSTACK_GROWTH +1`) exist only in
  `portable/SDCC/Cygnal`. No GCC port and no QEMU machine can host it; GDR
  therefore treats stacks as grow-down only (the `stack_grows_up` field and its
  dead branch were removed rather than kept as an untestable probe).
