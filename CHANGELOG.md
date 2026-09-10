# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- FreeRTOS object support: six-channel object discovery (registry, symbol,
  active timer lists, MPU pool, waiter, user) with provenance (`Src` cells),
  per-kind list tables and singular details for queues, semaphores, mutexes,
  timers, event groups and stream buffers; `frt objects` shows a
  `Kind/Count/Sources` summary.
- FreeRTOS `frt heap` renders the real heap snapshot: algorithm
  (heap_1..heap_5 plus heap_3/none), kernel counters, protector canary
  decode, free-list and linear walks with a three-way `CrossCheck` verdict.
  `frt system` reports `Heap used/total/status`.
- RT-Thread `rtt objects` command (registry-backed Kind/Count summary),
  symmetric with `frt objects`.
- Kinds without a value now render `N/A` uniformly (was a mix of `N/A` and
  `unavailable`); a verdict with a reason keeps the `unavailable:` prefix.

### Changed

- `heap` and `objects` render through the neutral `gdr.commands` renderers
  (`HeapReport` + `object_summary_table` adapter protocol), so `rtt` and
  `frt` share one code path and cannot drift.
- FreeRTOS kernel ABI names (struct member paths, globals, kinds) are
  consolidated into `freertos/layout.py` with `symbols`/kind tables;
  consumers read through the layout, never raw `read_path` tuples.
- Repetitive adapter helpers moved into `gdr/` (`arch_or_default`,
  `value_at`, `read_field_at`, `loadable_ranges`, command completion), and
  the FreeRTOS `timers ↔ details` import cycle was removed.
- Fixture builders share `ci/freertos/fixture-common.sh` and
  `fixture/common/` sources; the three FreeRTOS CI lanes and the local
  `ci/validate-podman.sh` reproducer now stay in sync.

### Fixed

- `gdr init` no longer kills the GDB session on policy failures (unknown
  RTOS name, invalid or unsupported version, declared/target version
  mismatch): it warns and returns to the prompt with no adapter registered.
- `rtt` and `frt` heap/system output uses a single `N/A` sentinel; a bare
  `unavailable` cell is rejected by a boundary test.

## [2026.02] - 2026-08-22

### Added

- Added RTOS-neutral `$gdr_task`, `$gdr_tasks`, and `$gdr_object` functions,
  plus adapter-owned task/object tables and a unified command router.
- Added RT-Thread detail commands for tasks, timers, IPC, and memory pools,
  including bounded waiter, capacity, policy, expiry, and consistency data.
- Added width-aware table rendering, target-symbolized function pointers, and
  a profile-driven QEMU/GDB harness with persistent sessions and diagnostics.
- Added RT-Thread heap command for heap info collection and diagnostic (heap
  algorithm, used size, total size, owner, corrupt check, memory bubble state).

### Changed

- Release archives now contain both RT-Thread and FreeRTOS adapters in one
  `gdr` asset. The `gdr` command is limited to initialization and help; RTOS
  commands remain under `rtt` and `frt`.
- Task and IPC tables expose capability-aware columns such as `BasePrio`,
  `Addr`, `CPU`, `Bind`, waiter lists, `Free`, `Used`, and `ExpiresIn`.
- Table widths shrink only marked text columns and preserve the complete
  column set across terminal sizes.

### Fixed

- Completed RT-Thread routing and object tables for events, mailboxes, message
  queues, and memory pools.
- Fixed SMP CPU-0 reporting and retained detail diagnostics when raw fields or
  waiters are unavailable; unsupported values now render explicitly as `N/A`.

## [2026.01] - 2026-07-29

### Added

- RT-Thread 3.1.x and 4.x support, with layout and kernel configuration
  probing for SMP, heap managers, and IPC components.
- GDB aggregate commands, pretty-printers, and convenience functions for
  RT-Thread objects.
- Closed-loop QEMU verification for Cortex-A9 and RISC-V RV64 targets.

### Changed

- Function-pointer columns in `rtthread threads` and `rtthread timers` now
  resolve target symbols while preserving a hexadecimal address fallback.
- GDB command registration and table output now provide clearer diagnostics
  for malformed target data and unavailable symbols.
