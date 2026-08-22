# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

No changes yet.

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
