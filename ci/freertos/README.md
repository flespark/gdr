# FreeRTOS fixture builds

Firmware and static-image builders for GDR's FreeRTOS closed-loop tests, plus
the facts that constrain them. Two builders exist, one per live lane, and the
kernel-direct builder also produces the static `snapshot` variant:

| Lane | Builder | Kernel source | What only this lane can prove |
| --- | --- | --- | --- |
| CubeL4 live | `build-fixture-cubel4.sh` | FreeRTOS 10.3.1 bundled in STM32CubeL4 `v1.18.2` | config branches on real running firmware (heap manager, trace facility, static allocation, runtime stats, queue sets, registry size) |
| Kernel-direct live | `build-fixture-kernel.sh` | `FreeRTOS-Kernel` at a git tag | version branches (notification array, mini list item, V11 heap protector, array-typed run-time totals) |
| Static snapshot (kernel-direct variant) | `build-fixture-kernel.sh --variant snapshot` | kernel headers only, data written by `fixture/config/snapshot/snapshot.c` | states a healthy kernel cannot produce: SMP decoding, corrupted structures, mismatched counters |

`run-qemu-matrix.sh [<target>] [<version>] [<variant>...]` drives the live
lanes and the file-only `snapshot` variant: it reuses a cached fixture when one
exists, otherwise builds it and installs it into the cache, then runs
`tests/integration/freertos`. The snapshot is an ordinary variant on the cell
`mps2-an521/11.1.0` — the negative-testing arm for data-corruption scenarios —
started the same way as a live cell: `run-qemu-matrix.sh mps2-an521 11.1.0
snapshot`.

## Fixture cache

Every builder installs its artifacts into one cache root so tests, the matrix
runner and other machines read the same layout:

```text
$FREERTOS_FIXTURE_CACHE/                 default ~/Project/gdr-fixture/freertos
  <target>/<version>/<variant>/freertos.elf   (+ .bin, + .map)
  mps2-an521/11.1.0/snapshot/snapshot_heap.elf   (second artifact of the snapshot cell)
```

`--cache-dir DIR` overrides the destination, `--no-cache-install` builds
without copying, and `GDR_FORCE_BUILD=1` makes the matrix rebuild even when a
cached fixture is present. A missing artifact makes the corresponding test
`pytest.skip`, so partial caches stay usable.

`run-qemu-matrix.sh` reuses a cached ELF only while it is newer than the fixture
sources it was built from (`fixture/main.c`, `fixture/config/gdr_fixture_common.h`,
`fixture/config/<variant>/`, `fixture/board/<target>/`, and the two builders);
otherwise it rebuilds and logs `cached fixture is older than its sources;
rebuilding`. Reason: a fixture edit silently invalidates every variant that is
not rebuilt, and the resulting failures point at the decoder rather than at the
cache -- a stale image can also *pass* obsolete assertions, hiding a real
regression.

## Toolchain

Both builders resolve their compiler from `--toolchain-path`,
then `RTOS_TOOLCHAIN_PATH` / `XPACK_ARM_TOOLCHAIN_PATH`, then `PATH`
(`arm-none-eabi-gcc` everywhere except the RISC-V lane's `riscv-none-elf-gcc`).

The GDB used by the tests (`GDR_GDB`) must have an embedded Python
interpreter, which is not implied by the toolchain name:

- xPack's `arm-none-eabi-gdb` reports "Python scripting is not supported in
  this copy of GDB"; the sibling `arm-none-eabi-gdb-py3` is the Python build.
- `gdb-multiarch` (Linux) and Homebrew's `gdb` (macOS) both work.

Probe a candidate rather than trusting its name:

```bash
"$GDR_GDB" --nx --quiet --batch --ex 'python print("ok")'
```

## Live lanes

### CubeL4 (`b-l475e-iot01a`)

`build-fixture-cubel4.sh` sparse-clones STM32CubeL4 `v1.18.2` (default
`/tmp/stm32cubel4-v1.18.2`) and compiles the shared fixture sources against the
FreeRTOS submodule that ships with it, so this lane is pinned to kernel
10.3.1 - the cache coordinates `b-l475e-iot01a/10.3.1` are constants, not
options. Startup code, linker script and `system_stm32l4xx.c` come from the
`FreeRTOS_LowPower_LPTIM` example project; the fixture itself uses the
Cortex-M SysTick port (`portable/GCC/ARM_CM4F`) and QEMU semihosting, because
QEMU does not model the board's LPTIM.

Because the kernel version is fixed, this is the lane that carries the config
variant matrix (`fixture/config/<variant>/FreeRTOSConfig.h`).

### Kernel-direct (`mps2-an385`)

`build-fixture-kernel.sh` shallow-clones `FreeRTOS-Kernel` at a tag (default
`/tmp/gdr-freertos-kernel-source`, override with `--kernel-dir` or
`FREERTOS_KERNEL_DIR`) and links it with `portable/GCC/ARM_CM3` plus
`fixture/board/mps2-an385/`. QEMU machine is `qemu-system-arm -M mps2-an385`.
A caller-supplied checkout is copied into the build directory before
`git checkout`, so a developer's reference tree is never mutated.

This lane carries the version matrix. The tags matter because they bracket ABI
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

### Kernel-direct SMP (`mps2-an521`)

`build-fixture-kernel.sh` with `--target mps2-an521` links the same shared
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
(10.3.1 CubeL4 and the 11.1.0 kernel-direct build). The tick wraps every
~65.5 s at 1000 Hz, so no assertion may hard-code absolute tick values.

## Static snapshot variant

`build-fixture-kernel.sh --variant snapshot` compiles
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
triple disagrees, and corrupted scheduler lists (`gdr_bad_*`). The test
module rebuilds a cached ELF when its sources are newer (same
source-newer-than-cache rule as the live lanes), so stale cached negatives
cannot silently satisfy new assertions.

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
report zero free words - a fake friendlier than the real thing, which would hide
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
