# Testing

GDR verifies every layer at two levels: fast host-side unit tests for the
hardware-independent core and adapter logic (`tests/unit/`), and closed-loop
QEMU/GDB integration tests that boot real RTOS firmware or inspect crafted
kernel images (`tests/integration/`). Fixture build mechanics live in
`ci/<rtos>/README.md`; this document defines the shared test infrastructure
and the per-RTOS integration test coverage.

## Unit test coverage boundary

`uv run pytest tests/unit --cov` enforces branch coverage (`fail_under = 85`)
across all 20 hardware-independent modules in `core/`, `rtthread/`, and
`freertos/` (`adapter`, `api`, `commands`, `constants`, `formatting`,
`heapmap`, `help`, `version`, `layout`, `stackpc`, `views`, `diagnostics`,
`objects`, `heap`). Only the four direct `gdb.Value` / DWARF / target-memory
traversal modules (`core/gdb_bridge.py`, `core/layout.py`,
`rtthread/navigation.py`, `freertos/navigation.py`) are excluded from the
unit-only gate and verified end-to-end by the closed-loop QEMU suites below.

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

## Shared closed-loop infrastructure

Closed-loop tests guard against silent output drift where Python helpers run
without raising an exception but decode the wrong kernel state. Every live
QEMU lane exercises the same four surfaces:

- **Pretty-printers** register once and fold kernel structs into one-line
  summaries (`p`, `bt full`, `info locals`).
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

`tests/support/loader.py` is the single reader of closed-loop environment
variables (`GDR_RTOS`, `GDR_VERSION`, `GDR_QEMU_TARGET`,
`GDR_FIXTURE_VARIANT`, cache roots, and ELF overrides). `tests/integration/conftest.py`
selects the target profile and assembles the session fixtures. Expected
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
| `mps2-an385` | FreeRTOS | `qemu-system-arm -M mps2-an385 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit Cortex-M3 |
| `mps2-an521` | FreeRTOS | `qemu-system-arm -M mps2-an521 -kernel freertos.elf -semihosting-config enable=on,target=native` | `freertos.elf` | 32-bit dual Cortex-M33 (SSE-200) |
| `qemu-virt-rv64` | FreeRTOS | `qemu-system-riscv64 -M virt -cpu rv64 -m 256M -bios freertos.bin -semihosting-config enable=on` | `freertos.elf` | 64-bit RISC-V (`riscv:rv64`) |

On 64-bit RISC-V lanes (`rv64` and `qemu-virt-rv64`), QEMU boots the raw
`.bin` image via `-bios` while GDB loads the companion DWARF `.elf`.

---

## RT-Thread integration testing (`tests/integration/rtthread/`)

### Fixture and version matrix

RT-Thread fixtures are built by applying platform- and version-scoped patch
series (`ci/rt-thread/patches/<target>/<version>/`) to upstream RT-Thread BSPs
and caching `<RT_THREAD_FIXTURE_CACHE>/<target>/<version>/rtthread.elf` (plus
`rtthread.bin` on RV64):

- **`cortex-a9` (`3.1.0–3.1.5`, `4.0.0–4.1.1`)**: Runs the full integration
  suite across representative releases, including `3.1.0`, `3.1.3`, and `3.1.5`
  to pin the pre-`Null` `rt_object_class_type` enum (`<= 3.1.2`), the
  `3.1.3` enum shift, the `3.1.4` addition of
  `rt_messagequeue.suspend_sender_thread` (dropped in `4.0.0–4.0.1` and
  restored in `4.0.2`), and the `4.0.x` to `4.1.x` layout transitions.
  Cortex-A9 patches set `RT_NAME_MAX` to 16 so canonical fixture names match
  the RV64 BSPs.
- **`rv64` (`4.0.4`, `4.0.5`, `4.1.0`, `4.1.1`)**: Verifies 64-bit pointer
  alignment, 64-bit switch-frame `PC` recovery, and BSP path transitions
  (`bsp/qemu-riscv-virt64` through `4.1.0`, `bsp/qemu-virt64-riscv` in
  `4.1.1`). Upstream has no QEMU RV64 BSP for `3.1.x`.

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

---

## FreeRTOS integration testing (`tests/integration/freertos/`)

### Fixture and variant matrix

FreeRTOS fixtures are built directly from `FreeRTOS-Kernel` git tags by
`ci/freertos/build-fixture-kernel.sh`, combining shared application code
(`ci/freertos/fixture/main.c`), board startup (`fixture/board/<target>/`),
and per-variant headers (`fixture/config/<variant>/FreeRTOSConfig.h`) into
`<FREERTOS_FIXTURE_CACHE>/<target>/<version>/<variant>/freertos.elf`.
Verification spans three complementary execution arms:

1. **Config-variant and version matrix on Cortex-M3 (`mps2-an385`)**:
   - **14 config variants at `10.3.1`**: `base` (default backward-compatible
     `pvContainer`, `heap_4`, trace on, no `pxEndOfStack`), `full`
     (`pxEndOfStack`, runtime stats, queue sets), `static-only` (no heap),
     `static-dynamic`, `trace-off` (no `ucQueueType`, no stack prefill),
     `heap-1`, `heap-2`, `heap-3` (libc wrapper), `heap-5`, `registry-0`,
     `pend-callback` (`xTimerPendFunctionCall` queued on daemon), `streams`
     (stream/message/batching ring wrap and deleted-buffer state), and
     `tick16` (16-bit `TickType_t`).
   - **Version sweep (`10.4.6`, `10.5.1`, `11.1.0`)**: Exercises task
     notification arrays (`>= 10.4`), `configUSE_MINI_LIST_ITEM`,
     `heap-protector` and `heap-5-protector` canary decoding (`>= 11.0`), and
     stream-buffer `uxNotificationIndex` (`>= 11.1`).
2. **Architecture, SMP, and MPU lanes (`mps2-an521` and `qemu-virt-rv64`)**:
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

## Summary comparison of RT-Thread vs FreeRTOS test strategies

| Dimension | RT-Thread (`ci/rt-thread/`) | FreeRTOS (`ci/freertos/`) |
| --- | --- | --- |
| **Firmware source** | Upstream RT-Thread repo + per-BSP/version patch sets | Upstream `FreeRTOS-Kernel` tags + shared `fixture/main.c`, `board/`, `config/<variant>/` |
| **Primary matrix axis** | Kernel release history (`3.1.0–3.1.5`, `4.0.0–4.1.1`) across 32-bit ARM and 64-bit RV64 | Compile-time `FreeRTOSConfig.h` feature matrix (17 variants) + kernel version sweep (`10.3.1–11.3.1`) |
| **QEMU targets** | `cortex-a9` (`vexpress-a9`), `rv64` (`virt`) | `mps2-an385` (CM3), `mps2-an521` (dual CM33 SMP / MPU), `qemu-virt-rv64` (RV64) |
| **Negative / corruption testing** | Unit tests + reset-vector `NULL`-head startup session (`test_startup.py`) | Dedicated file-only static ELF pair (`test_snapshot.py`: `freertos.elf` + `snapshot_heap.elf`) |
| **Expectation oracle** | `tests/support/rtthread_fixture_profiles.py` | `tests/support/freertos_fixture_profiles.py` |
