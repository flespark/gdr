# Testing

GDR verifies every layer at two levels: fast host-side unit tests for the
hardware-independent core and adapter logic (`tests/unit/`), and closed-loop
QEMU/GDB integration tests that boot real RTOS firmware or inspect crafted
kernel images (`tests/integration/`). Fixture build mechanics live in
[the shared CLI guide](../ci/README.md) and the RTOS lane READMEs; this document defines the
shared test infrastructure and the per-RTOS integration test coverage.

## Unit test coverage boundary

`uv run pytest tests/unit --cov` enforces branch coverage (`fail_under = 85`)
across the configured hardware-independent modules in `core/`, `rtthread/`, and
`freertos/` (`adapter`, `api`, `commands`, `constants`, `formatting`,
`heapmap`, `help`, `version`, `layout`, `stackpc`, `views`, `diagnostics`,
`objects`, `heap`, `elf`, `stack_frame_parser`, and the `backtrace/`
subpackages (`core/backtrace/`, `freertos/backtrace/`, `rtthread/backtrace/`)).
Direct `gdb.Value` / DWARF / target-memory traversal modules
(`core/gdb_bridge.py`, `core/layout.py`,
`rtthread/navigation.py`, `freertos/navigation.py`) and the two
`backtrace/backtrace_gdb.py` orchestration modules remain verified via focused
orchestration unit tests and closed-loop QEMU suites.

In addition to functional unit tests, `tests/unit/core/test_boundary.py`
enforces repository-wide structural invariants on every run:

- `core/` contains zero RTOS-specific identifiers and never imports
  an adapter package.
- Production modules stay within the 1,500-line ceiling.
- Kernel symbol/type literals stay inside their owning domain modules
  (`layout.py`, `navigation.py`, `diagnostics.py`, `objects.py`, `heap.py`).
- Adapter code uses `read_field` / `value_at` / bridge primitives rather than
  inline `read_path`, raw `gdb.lookup_type`, or raw DWARF `.sizeof` /
  `.fields()` access.
- The runtime has zero third-party dependencies and never imports the dev-only
  oracle packages `pyelftools` or `capstone`.

`tests/unit/core/test_oracle_*.py` cross-check the bounded ELF/section, CFI,
EHABI, DWARF-expression and Thumb/A32/RISC-V decoders against those two
oracles. Deterministic in-memory corpora always run in CI; cached firmware ELFs
under `ci/.cache/fixtures` are scanned only when the local cache is present.

## Shared closed-loop infrastructure

Closed-loop tests guard against silent output drift where Python helpers run
without raising an exception but decode the wrong kernel state. Every live
QEMU lane exercises the same four surfaces:

- **Pretty-printers** register once and fold kernel structs into one-line
  summaries (`p task_name`).
- **Convenience functions** (`$gdr_task`, `$gdr_tasks`, `$gdr_object`) return
  non-null target-native `gdb.Value` objects with expected struct fields.
- **Aggregate commands** (`rtt ...` / `frt ...`) render complete tables,
  singular `Key: Value` details, heap reports, consistency checks, and
  stop-time watches.
- **Machine-readable API** (`gdr api <verb>`, `$gdr_json`, `$gdr_json_chunk`,
  `$gdr_dump`) round-trips valid JSON envelopes, reassembles chunked frames,
  and preserves typed cell values and source/address navigation metadata
  (`tests/integration/test_panel_tables.py`).

### Persistent GDB session harness

Live integration suites use a persistent `pexpect`-driven GDB session rather
than spawning a batch GDB process per test:

1. A session-scoped `QemuSession` (`tests/support/qemu_harness.py`) starts
   the target's QEMU machine with a dynamically allocated `-gdb tcp::<port>`
   endpoint (free-running, no `-S`) and waits for the firmware's serial
   ready marker. Distinct serial and QEMU logs are captured for boot-timeout
   and early-exit diagnostics.
2. A session-scoped `GdbSession` spawns one GDB process via `pexpect`,
   connects to QEMU, and runs `source gdr.py` **once**, keeping pretty-printers,
   convenience functions, and session state registered across the entire suite.
3. Each test calls `gdb_session.run(...)` to execute GDB commands and capture
   stripped output.

The [shared integration CLI](../ci/README.md) owns cache initialization,
source preparation, firmware manifests, tool preflight and pytest execution.
`tests/support/loader.py` reads its session transport (`GDR_RTOS`,
`GDR_VERSION`, `GDR_QEMU_TARGET`, `GDR_FIXTURE_VARIANT`, and GDB/QEMU settings).
Firmware paths are fixed under `ci/.cache/fixtures/<rtos>/`; pytest never
builds or downloads artifacts. `tests/integration/conftest.py` selects the
target profile and assembles the session fixtures. Expected
capabilities and object layouts for each fixture live in independent test
profiles (`tests/support/rtthread_fixture_profiles.py` and
`tests/support/freertos_fixture_profiles.py`) that never import production
`layout.py` code, so a layout regression cannot self-update the expected test
answers.

### QEMU target profiles

| Target | RTOS | QEMU startup | GDB symbols | Pointer / ABI |
| --- | --- | --- | --- | --- |
| `cortex-a9` | RT-Thread | `qemu-system-arm -M vexpress-a9 -kernel rtthread.elf` | `rtthread.elf` | 32-bit ARM |
| `rv64` | RT-Thread | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios rtthread.bin` | `rtthread.elf` | 64-bit RISC-V (`riscv:rv64`) |
| `mps2-an385` | RT-Thread | `qemu-system-arm -M mps2-an385 -kernel rtthread.elf` | `rtthread.elf` | 32-bit Armv7-M (Cortex-M3) |
| `microbit` | RT-Thread | `qemu-system-arm -M microbit -global nrf51-soc.sram-size=262144 -kernel rtthread.elf` | `rtthread.elf` | 32-bit Armv6-M (Cortex-M0) |
| `mps2-an505` | RT-Thread | `qemu-system-arm -M mps2-an505 -kernel rtthread.elf` | `rtthread.elf` | 32-bit Armv8-M Mainline (single Cortex-M33) |
| `mps2-an386` | RT-Thread | `qemu-system-arm -M mps2-an386 -kernel rtthread.elf` | `rtthread.elf` | 32-bit Armv7E-M (single Cortex-M4; `soft`/`softfp`/`fpu`) |
| `rv32` | RT-Thread | `qemu-system-riscv32 -M virt -cpu rv32 -m 256M -bios rtthread.bin` | `rtthread.elf` | 32-bit RISC-V (`riscv:rv32`) |
| `mps2-an385` | FreeRTOS | `qemu-system-arm -M mps2-an385 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit Armv7-M (Cortex-M3) |
| `mps2-an386` | FreeRTOS | `qemu-system-arm -M mps2-an386 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit Armv7E-M (single Cortex-M4; `soft`/`softfp`/`fpu`) |
| `mps2-an505` | FreeRTOS | `qemu-system-arm -M mps2-an505 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit Armv8-M Mainline (single Cortex-M33) |
| `mps2-an521` | FreeRTOS | `qemu-system-arm -M mps2-an521 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit Armv8-M Mainline (dual Cortex-M33, SSE-200) |
| `microbit` | FreeRTOS | `qemu-system-arm -M microbit -global nrf51-soc.sram-size=262144 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit Armv6-M (Cortex-M0, Thumb-1) |
| `qemu-virt-rv32` | FreeRTOS | `qemu-system-riscv32 -M virt -cpu rv32 -m 256M -bios freertos.bin -semihosting-config enable=on` | `freertos.elf` | 32-bit RISC-V (`riscv:rv32`) |
| `qemu-virt-rv64` | FreeRTOS | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios freertos.bin -semihosting-config enable=on` | `freertos.elf` | 64-bit RISC-V (`riscv:rv64`) |

On RISC-V lanes, QEMU boots the raw
`.bin` image via `-bios` while GDB loads the companion DWARF `.elf`.

---

## RT-Thread integration testing (`tests/integration/rtthread/`)

### Fixture and version matrix

RT-Thread fixtures are built by applying platform- and version-scoped patch
series (`ci/fixture/rtthread/patches/<target>/<version>/`) to upstream RT-Thread BSPs
and caching `ci/.cache/fixtures/rtthread/<target>/<version>/rtthread.elf` (plus
`rtthread.bin` on RISC-V):

- **`cortex-a9` (`3.1.0–3.1.5`, `4.0.0–4.1.1`)**: Runs the full integration
  suite across representative releases, including `3.1.0`, `3.1.3`, and `3.1.5`
  to pin the pre-`Null` `rt_object_class_type` enum (`<= 3.1.2`), the
  `3.1.3` enum shift, the `3.1.4` addition of
  `rt_messagequeue.suspend_sender_thread` (dropped in `4.0.0–4.0.1` and
  restored in `4.0.2`), and the `4.0.x` to `4.1.x` layout transitions.
  The default matrix compiles 3.1.1, 3.1.2, and 3.1.4 only; explicit CLI
  runs can execute them. Cortex-A9 patches set `RT_NAME_MAX` to 16 so canonical fixture names match
  the RV64 BSPs.
- **`rv64` (`4.0.4`, `4.0.5`, `4.1.0`, `4.1.1`)**: Verifies 64-bit pointer
  alignment, 64-bit switch-frame `PC` recovery, and BSP path transitions
  (`bsp/qemu-riscv-virt64` through `4.1.0`, `bsp/qemu-virt64-riscv` in
  `4.1.1`). Upstream has no QEMU RV64 BSP for `3.1.x`.
- **`mps2-an385` (`4.0.5`)**: Runs the shared integration suite and independent
  compiled-port/saved-frame checks on an RT-Thread-owned BSP. Build inputs live
  in the [AN385 fixture guide](../ci/fixture/rtthread/README.md#mps2-an385-armv7-m).
- **`microbit` (`4.0.5`)**: Runs the shared suite and raw port-compatibility
  checks on an RT-Thread-owned BSP. Build inputs and the Nordic tick source
  live in the [microbit fixture guide](../ci/fixture/rtthread/README.md#microbit-armv6-m).
- **`mps2-an505` and `rv32` (`4.0.5`)**: Run the shared suite and independent
  80/128-byte port-frame checks, initial entry/exit checks and read-only current
  chains. Build inputs live in the [AN505](../ci/fixture/rtthread/README.md#mps2-an505-armv8-m-mainline)
  and [RV32](../ci/fixture/rtthread/README.md#rv32-rv32) fixture guides.

Each fixture spawns a deterministic set of healthy and blocked worker threads
(`worker1`..`worker11`, `locker`) contended on canonical IPC objects
(`test_sem`/`wait_sem`, `test_mutex`/`wait_mutex`, `test_event`/`wait_event`,
`test_mailbox`/`wait_mb_recv`/`wait_mb_send`,
`test_msgqueue`/`wait_mq_recv`/`wait_mq_send`, `test_mempool`/`wait_mp`,
`test_timer`).

### RT-Thread suite organization and capability coverage

| Test module | Verified capabilities |
| --- | --- |
| `test_printers.py` | One-line pretty-printer folds for `rt_thread`, `rt_semaphore`, `rt_mutex`, `rt_event`, `rt_mailbox`, `rt_messagequeue`, `rt_mempool`, and `rt_timer`; symbolic `ThreadState` names and timer/IPC flag decoding. |
| `test_functions.py` | `$gdr_task`, `$gdr_tasks`, and `$gdr_object` returning native `gdb.Value` structs by name or address; version-aware `suspend_sender_thread` field presence on message queues. |
| `test_commands.py` | Every `rtt` list and detail command (`threads`/`thread`, `semaphores`, `mutexes`, `events`, `mailboxes`, `msgqueues`, `mempools`, `timers`, `objects`, `system`, `heap`, `heap dump`, `check`, `watch`/`unwatch`), waiter chains (`count@names`), mailbox slot addresses, timer `Src` provenance (`registry+active`), and proven saved-frame `PC`. |
| `test_api.py` | `$gdr_json`, `$gdr_json_chunk`, `$gdr_dump`, `gdr api <verb>`, `gdr probe` evidence, and `gdr reset` / `--force` / `--allow-unverified` session lifecycle. |
| `test_startup.py` | Separate paused-at-reset QEMU session verifying that snapshots taken before kernel initialization safely stop on `NULL` timer-list heads instead of fabricating wrapped object addresses, and recover once the scheduler starts. |
| `test_backtrace.py` | Saved/current chains (including `soft`/`softfp`/`fpu` ABI coverage on AN386 and `worker_fpu` FPU frames on AN386/AN505), unstarted roots, exit completeness, external IRQ & SysTick cross-stack unwinding, real Arm M-profile and RISC-V (`rv32`/`rv64`) scheduler/trap transition stages (including `rt_hw_context_switch_to`, `rt_hw_context_switch`, `trap_entry` prologue/epilogue switches, `rt_hw_context_switch_exit`/`spurious_interrupt`, and E310 `nested-trap-epc-unproven`), deterministic `synchronous-fault` hook frames, `NMI`/`DebugMonitor` refusals, bounded output, structured refusals, and unchanged target state. |
| `test_fixtures.py` | Consolidated port/fixture oracles: basic Armv6-M/Armv7-M instructions, DWARF offsets, raw frames and bounded MSP/heap/vector evidence; Armv8-M Mainline 80-byte DWARF/Secure header/raw-seed exit checks; RV32 128-byte DWARF/raw-seed exit checks; and `microbit` PendSV `0xFFFFFFFD` return, TIMER2 tick registers, IRQ10 vector and unused SysTick vector. |

---

## FreeRTOS integration testing (`tests/integration/freertos/`)

### Fixture and variant matrix

FreeRTOS fixtures are built directly from `FreeRTOS-Kernel` git tags by
`ci/fixture/freertos/build.sh`, combining shared application code
(`ci/fixture/freertos/profiles/main.c`), board startup (`profiles/board/<target>/`),
and per-variant headers (`profiles/config/<variant>/FreeRTOSConfig.h`) into
`ci/.cache/fixtures/freertos/<target>/<version>/<variant>/freertos.elf`.
Verification spans three complementary execution arms:

1. **Config-variant and version matrix on Armv7-M (`mps2-an385`)**:
   - **13 config variants at `10.3.1`**: `base` (default backward-compatible
     `pvContainer`, `heap_4`, trace on, no `pxEndOfStack`), `full`
     (`pxEndOfStack`, runtime stats, queue sets, plus gated IRQ/scheduler/fault
     fixtures), `static-only` (no heap),
     `static-dynamic`, `trace-off` (no `ucQueueType`, no stack prefill),
     `heap-1`, `heap-2`, `heap-3` (libc wrapper), `heap-5`, `registry-0`,
     `pend-callback` (`xTimerPendFunctionCall` queued on daemon), `streams`
     (stream/message/batching ring wrap and deleted-buffer state), and
     `tick16` (16-bit `TickType_t`).
   - **Version sweep (`10.4.6`, `10.5.1`, `11.1.0`)**: Exercises task
     notification arrays (`>= 10.4`), `configUSE_MINI_LIST_ITEM`,
     `heap-protector` and `heap-5-protector` canary decoding (`>= 11.0`), and
     stream-buffer `uxNotificationIndex` (`>= 11.1`).
2. **Architecture, SMP, and MPU lanes**:
   - **Dual-core SMP (`mps2-an521` / `11.3.1` / `smp`)**: Boots two
     Cortex-M33 cores on SSE-200 (`cpu1_start.c` wakes CPU1 via `INITSVTOR1`
     then `CPUWAIT`) and verifies shared-memory `pxCurrentTCBs[]`, `CPU`, and
     `Affinity` columns without requiring `target extended-remote`.
   - **MPU wrappers v2 (`mps2-an521` / `11.3.1` / `mpu`)**: Exercises
     `xKernelObjectPool` object discovery and external-index-to-handle
     translation.
   - **64-bit RISC-V (`qemu-virt-rv64` / `11.1.0` / `rv64`)**: Verifies
     64-bit pointers, 64-bit `TickType_t` event-group control masks, and
     64-bit heap allocation-bitmask arithmetic.
   - **Armv6-M (`microbit` / `11.1.0` / `full`)**: Builds the shared fixture
     against `portable/GCC/ARM_CM0` with the expanded QEMU memory setup (see
     the [lane README](../ci/fixture/freertos/README.md#microbit-armv6-m));
     verifies the Thumb-1 context frame, EHABI/CFI selection, IRQ/SysTick handler
     unwinding, `fault-frame-imprecise` refusal, and Armv6-M scheduler transitions.
   - **Armv7E-M (`mps2-an386` / `11.3.1` / `fpu`, `softfp`, `soft`)**: Builds
     `portable/GCC/ARM_CM4F` (`fpu`, `softfp`) and `portable/GCC/ARM_CM3`
     (`soft`) on Cortex-M4; verifies `aapcs32` integer and `aapcs32`/`aapcs32-vfp`
     extended FPU task/handler frames (`0xFFFFFFE1/E9/ED`), synchronous faults,
     SysTick, and CM3/CM4F scheduler stages.
   - **Single-core Armv8-M Mainline (`mps2-an505` / `11.3.1` / `full`)**: Builds
     single-core `ARM_CM33_NTZ/non_secure` with single-Secure-state QEMU execution;
     verifies external IRQ `19`/`20`, SysTick, synchronous faults, and CM33 scheduler transitions.
   - **RISC-V (`qemu-virt-rv32` `10.3.1`/`10.4.6`/`11.1.0`/`11.3.1` `base`, `qemu-virt-rv64` `10.4.6`/`11.1.0`/`11.3.1` `rv64`)**:
     Builds `rv32imac_zicsr` (`ilp32`) and `rv64imac_zicsr` (`lp64`, 64-bit ticks);
     verifies saved/live CFI unwinding across `10.3.x–11.3.1` trap layouts, M-mode IRQ traps, synchronous `ecall` yield,
     `xPortStartFirstTask` first-start stages, `vTaskSwitchContext` before/after TCB
     writeback, `processed_source` restore stages, and fail-closed non-`ecall` sync faults.
3. **File-only static snapshot for corruption negatives (`mps2-an521/11.1.0/snapshot`)**:
   - Loaded via `StaticElfSession` (`tests/support/freertos_elf_harness.py`)
     using GDB `file` only (no QEMU). Pre-initialized `.data` structures in
     `freertos.elf` and `snapshot_heap.elf` falsify diagnostic checks that a
     healthy running kernel cannot produce: corrupt scheduler lists,
     `ListIntegrityBytes` failures, a timer on the overflow list, heap
     free-list/linear/counter mismatches, and a free-list block carrying the
     allocated MSB.

### FreeRTOS suite organization and capability coverage

| Test module | Verified capabilities |
| --- | --- |
| `test_boot.py` & `test_variants.py` | Retained DWARF struct visibility (`tskTaskControlBlock`, `QueueDefinition`, `tmrTimerControl`), target pointer width, and live `detect_config()` parity against every field of `FreeRtosTestProfile`. |
| `test_printers.py` & `test_functions.py` | Typedef-spelled `Task` / `List` / `Queue` folds; `$gdr_task`, `$gdr_tasks`, and `$gdr_object` resolution across all six discovery channels. |
| `test_commands.py` & `test_api.py` | Full `frt` command tree and JSON envelope parity across variants: task watermark/runtime/notification details, queue FIFO `Item[0]` addresses and `Set` column, `?` inferred-kind markers on `trace-off`, recursive mutexes (`Held`/`Owner`/`Recursive`), timer daemon command queue (`pend-callback`), event-group waiter conditions (including stack-handle-only `gdr_waiter_only_eg_task` found exclusively via the waiter channel), stream/message/batching buffers, and `heap_1..5` + canary cross-checks. |
| `test_smp.py` | Dual-core `pxCurrentTCBs[0..1]` running-task resolution, `CPU` placement, core-affinity masks (`gdr_bound`), and preemption-disable state (`gdr_preempt`). |
| `test_snapshot.py` | File-only negative testing for `frt check` and inline `Check[...]` failure/mismatch/corrupt verdicts on `freertos.elf` and `snapshot_heap.elf`. |
| `test_startup.py` | Pre-task-creation vs post-scheduler-startup command output on `mps2-an385/full`. |
| `test_backtrace.py` | Saved-context and live chains across all ports, proven exit completeness versus partial RISC-V first-task chains, external IRQ & SysTick cross-stack unwinding (including extended FPU frames on `mps2-an386`), real M-profile PendSV/SVC and RISC-V `xPortStartFirstTask`/`ecall`/trap-prologue/`vTaskSwitchContext`/`processed_source` transition stages with raw oracles, deterministic synchronous faults (`synchronous-fault` on M3/M4/M33, `fault-frame-imprecise` on M0, `exception-frame-type-unproven` on non-`ecall` RISC-V), bounded depth, explicit refusals, and unchanged halted target state. |

### Exception unwind acceptance

Architecture and RTOS-port acceptance are separate requirements. Passing
one does not establish the other; synthetic frames and offline ELF mutations
test rejection behavior, not live port support.

| Layer | Acceptance requirements | Evidence |
| --- | --- | --- |
| Architecture | ISA-specific EXC_RETURN/IPSR widths and exception tables; shared eight-word frame register layout; Armv6-M mandatory 8-byte entry alignment versus Armv7-M CCR.STKALIGN; exact stacked PC, padding, bounds and independent budgets. | `core/backtrace/unwind_arm.py`, `tests/unit/core/test_unwind_armv6m.py`, `test_unwind_armv7m.py` and shared driver tests. |
| RTOS port | Proven task ownership and stack spans, target-specific capacity/vector evidence, ISA hook selection and fail-before-unwind refusals; actual IRQ, nested handler and handler C-call chains; raw frame oracles and unchanged target state. | `tests/unit/freertos/test_backtrace_armv6m.py`, `test_backtrace_gdb.py` and `tests/integration/freertos/test_backtrace.py`. |

The live IRQ tests select `freertos/microbit/11.1.0/full`, `mps2-an385/full`,
`mps2-an386/11.3.1/{fpu,softfp,soft}`, or `mps2-an505/11.3.1/full` (gated by `gdr_irq_gate`).
Their oracle reads GDB MSP/PSP, xPSR, CONTROL, raw hardware frames, task bounds
and linker symbols independently of GDR's transition result. Register,
task/IRQ-stack hashes, current TCB and tick snapshots must remain unchanged
across a query. Both lanes exercise single IRQs, nesting, ordinary handler calls,
four-byte alignment padding, vector-entry proof, missing MSP bounds and separate
exception budgets.

Armv6-M port tests require Cortex-M0 CPUID (`0x410CC200`), fixed-zero vectors and explicit
linker capacity; the live read oracle rejects ICTR, VTOR and CCR accesses.
Missing/conflicting CPUID, vector or capacity facts and missing
scheduler ownership fail closed. Reserved IPSR values, faults and scheduler
exceptions retain their separate refusal reasons. Armv7-M capacity tests check
ICTR upper bound 47 and reject malformed/unreadable ICTR or out-of-capacity
live/restored exception numbers. Only exception numbers 24/25 have live
vector/handler coverage; capacity acceptance of 47 is not a live handler proof.
Other ports and extended frames need their own fixtures. Build facts live in
the [microbit](../ci/fixture/freertos/README.md#microbit-armv6-m) and
[AN385](../ci/fixture/freertos/README.md#mps2-an385-armv7-m) fixture guides.

```bash
uv run pytest tests/unit/core/test_unwind_armv6m.py tests/unit/core/test_unwind_armv7m.py tests/unit/freertos/test_backtrace_armv6m.py tests/unit/freertos/test_backtrace_gdb.py
python3 ci/test.py run freertos microbit 11.1.0 full -- tests/integration/freertos/test_backtrace.py
python3 ci/test.py run freertos mps2-an385 11.1.0 full -- tests/integration/freertos/test_backtrace.py
```

### Scheduler unwind acceptance

The scheduler tests run across Arm M-profile (`freertos/mps2-an385/full`,
`freertos/microbit/11.1.0/full`, `freertos/mps2-an386/11.3.1/{fpu,softfp,soft}`,
`freertos/mps2-an505/11.3.1/full`) and RISC-V (`freertos/qemu-virt-rv32/{10.3.1,10.4.6,11.1.0,11.3.1}/base`,
`freertos/qemu-virt-rv64/{10.4.6,11.1.0,11.3.1}/rv64`, `rtthread/rv32/4.0.5`, `rtthread/rv64/4.0.5`). Tests
check compiled instruction bytes and raw TCBs, saved core registers, hardware
frames, PSP/MSP and xPSR independently of GDR's stage decision. Breakpoints
observe SysTick before PendSV, the current-TCB store before/after execution,
restored registers before PSP writeback, real timer preemption
before scheduling, and the first SVC path. Refusal tests cover unmatched
profiles and unproven stops. Unit/synthetic mutations and offline ELF
perturbations test rejection but do not establish additional live support.
Build facts live in the [scheduler fixture guide](../ci/fixture/freertos/README.md#mps2-an385-armv7-m);
the supported stages and exclusions live in [boundaries](boundary.md#task-backtraces) ([FreeRTOS](boundary.md#freertos-backtrace-matrix), [RT-Thread](boundary.md#rt-thread-backtrace-matrix)).

## Isolated `.eh_frame` variant

`tests/integration/bare/test_eh_frame.py` uses the
`bare/qemu-virt-rv64/none/eh-frame` cell. Its GNU-assembled RV64 images
have `.eh_frame` (plus companion `PT_GNU_EH_FRAME`/`.eh_frame_hdr` and
encoded-pointer fixtures), no `.debug_frame` or EHABI, and frame-pointer fallback
disabled. Fourteen tests verify an ordered four-frame chain with an extended
offset and virtual return column 128, a signal-CIE exact boundary PC, all
three CFI expression rules, rejection of a forbidden expression without
fallback, missing/truncated metadata or unavailable pointer bases, on-demand
`PT_GNU_EH_FRAME`/`.eh_frame_hdr` binary search (and safe fallback when the
index is damaged while still rejecting truncated `.eh_frame`), and live
resolution/rejection of `DW_EH_PE_textrel`, `DW_EH_PE_datarel`,
`DW_EH_PE_funcrel` and `DW_EH_PE_indirect` against QEMU RAM.
Undefined return columns remain partial endpoints.
Signal-CIE coverage does not verify hardware exception decoding or
cross-stack unwinding. Build details live in
[the fixture README](../ci/fixture/bare/eh-frame/README.md).

The fixture is RTOS-independent on purpose: it tests the shared `core`
CFI decoder, so one lane covers both adapters. The regular RV lanes
(`qemu-virt-rv32/base`, `qemu-virt-rv64/rv64`) carry toolchain-generated
`.eh_frame` themselves (`-funwind-tables` plus a linker `KEEP` so
`--gc-sections` does not discard it); the isolated fixture exists for the
adversarial encodings normal compiler output never emits.

The cell belongs to `bare` in the shared matrix, including CNB and
GitHub RTOS verification. Additional exception modes and scheduler
stages must add deterministic RTOS-owned fixture inputs and independent
assertions through the [same lane extension contract](../ci/README.md#layering-and-new-lanes).

## Summary comparison of RT-Thread vs FreeRTOS test strategies

| Dimension | RT-Thread (`ci/fixture/rtthread/`) | FreeRTOS (`ci/fixture/freertos/`) |
| --- | --- | --- |
| **Firmware source** | Upstream RT-Thread repo + per-BSP/version patch sets | Upstream `FreeRTOS-Kernel` tags + shared `profiles/main.c`, `board/`, `config/<variant>/` |
| **Primary matrix axis** | Kernel release history (`3.1.0–3.1.5`, `4.0.0–4.1.1`) across 32-bit ARM and 64-bit RV64 | Compile-time `FreeRTOSConfig.h` feature matrix (22 variants) + kernel version sweep (`10.3.1–11.3.1`) |
| **QEMU targets** | `cortex-a9` (`vexpress-a9`), `rv32`, `rv64` (`virt`), `microbit`, `mps2-an385`, `mps2-an386`, `mps2-an505` | `microbit` (Armv6-M), `mps2-an385` (Armv7-M), `mps2-an386` (Armv7E-M `soft`/`softfp`/`fpu`), `mps2-an505` (Armv8-M Mainline single-core), `mps2-an521` (Armv8-M Mainline SMP / MPU), `qemu-virt-rv32`, `qemu-virt-rv64` |
| **Negative / corruption testing** | Unit tests + reset-vector `NULL`-head startup session (`test_startup.py`) | Dedicated file-only static ELF pair (`test_snapshot.py`: `freertos.elf` + `snapshot_heap.elf`) |
| **Expectation oracle** | `tests/support/rtthread_fixture_profiles.py` | `tests/support/freertos_fixture_profiles.py` |
