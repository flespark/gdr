# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- FreeRTOS pretty-printers: `p *pxCurrentTCB` now folds to
  `Task(current_priority=0, name="IDLE", base_priority=0)`, with similar
  folds for `List(...)`, `Queue(...)`, `Timer(...)`, `EventGroup(...)`,
  `StreamBuffer(...)`, and `ListItem(...)`.
- FreeRTOS config probing: 14 new capability fields (`heap_kind`,
  `max_priorities`, `queue_registry_size`, `mpu_object_pool`,
  `list_integrity_check`, `event_groups`, `stream_buffers`,
  `stream_buffer_notification_index`, `queue_sets`, `task_attributes`,
  `preemption_disable`, `critical_nesting_in_tcb`, `posix_errno`,
  `delay_abort`, `heap_protector`).
- RTOS-neutral `read_macro_text(name)` bridge primitive for reading GDB
  macro text without CU-specific assumptions.

### Changed

- FreeRTOS supported version range is now continuous `(10,3,0)-(11,2,99)`;
  previously rejected versions such as 10.4.x, 10.7+, and 11.2.x are now
  accepted.
- Pretty-printer type matching now resolves typedef and cv-qualified values
  to their underlying struct tag (`strip_typedefs().unqualified().tag`),
  fixing fold for all FreeRTOS kernel objects which are typedef-spelled.

### Fixed

- StreamBuffer summary field path corrected from `uxLength` (Queue member)
  to `xLength` (actual `StreamBufferDef_t` member).
- Queue `type` summary field is now gated by `cfg.trace_facility`; builds
  with `configUSE_TRACE_FACILITY=0` no longer show `type=N/A`.
- `_array_bound` treats zero or negative DWARF range as unknown (`None`)
  instead of writing `0` into `number_of_cores` / `max_priorities`.
- FreeRTOS boundary regex in `test_boundary.py` tightened to catch long
  identifiers (`tsk[A-Za-z0-9_]+`).

### Removed

- `FreeRtosTask.entry` field and `struct xTASK_STATUS` layout (zero
  consumers; FreeRTOS TCB does not store entry function).
- `mpu_wrapper_v2` config field (replaced by `mpu_object_pool` with correct
  semantics).

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
