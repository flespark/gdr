# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- Width-aware ASCII table rendering: lists probe the current GDB/terminal
  width and shrink only marked elastic text columns (`Name`/`Owner`/`Waiters`/
  `Callback`/`Entry`) with two-dot truncation. The column set never changes
  with width, and tables restore the natural layout when minimum elastic
  widths still overflow.
- Singular object detail commands: `rtt thread|timer|semaphore|mutex|event|
  mailbox|messagequeue|mempool <name>` render one object as a vertical
  `Key: Value` block via the adapter-owned `object_detail` contract.
- Waiter summaries on IPC and mempool tables: `count:names` `Waiters` columns
  for semaphores, mutexes, events, and memory pools, plus split
  `RecvWait`/`SendWait` columns for mailboxes and message queues. Counts come
  from bounded, corruption-guarded suspend-list traversal (via
  `struct rt_thread.tlist`), never a cached counter removed in later kernels,
  and versions without a sender wait list render `N/A` instead of a fake `0`.
  Event detail pairs each waiter with its `event_set` mask and
  AND/OR/CLEAR mode.
- Derived capacity columns: mailboxes/message queues show `Free`, memory
  pools show `Used`, IPC objects decode a `FIFO`/`PRIO` policy column, mutex
  rows show `OrigPrio`, and timer rows add `Addr` plus a wrap-safe `ExpiresIn`
  (inactive timers render `N/A`).
- Task lists add `BasePrio` and `Addr`; SMP targets show `CPU`/`Bind` with
  the current task's real `oncpu`, and the CPU-0-to-`-1` coercion bug is
  fixed so CPU 0 is a valid value.
- Object detail diagnostics: thread detail shows `error`/`remaining_tick`,
  timer detail shows the callback `parameter`, message queues walk their
  message/free chains and verify `entry`/`max_msgs`, mailboxes list FIFO
  slots and validate ring offsets, and memory pools report pool range, block
  alignment, and free-list consistency. All walks are bounded and
  corruption-guarded.
- RTOS-neutral profile-driven QEMU/GDB harness with dynamic GDB ports,
  session-local logs, persistent GDB connections, and actionable boot errors.
- FreeRTOS Phase 1 package and a B-L475E-IOT01A QEMU fixture built
  from STM32CubeL4 v1.18.2 and its pinned FreeRTOS V10.3.1 submodule.
- RTOS-neutral semantic adapter API with raw-value `$gdr_task`, `$gdr_tasks`,
  and `$gdr_object(kind, name)` convenience functions. The `gdr` command is
  limited to `init` and `help`.
- FreeRTOS Phase 2 version/config probes, DWARF task layouts, safe scheduler
  navigation, and the RTOS-specific `freertos tasks/system` commands.
- RT-Thread `rtthread` / `rtt` command tree for task, system, semaphore, mutex,
  timer, and IPC object output, with an `rtt help` command that lists supported
  subcommands and aliases. The redundant `rtt objects` command was removed.
- FreeRTOS fixture smoke tests covering boot readiness, debug type visibility,
  32-bit ABI, persistent commands, task enumeration, and system counters.

### Fixed

- Completed RT-Thread command routing and object tables for events, mailboxes,
  message queues, and memory pools.

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
