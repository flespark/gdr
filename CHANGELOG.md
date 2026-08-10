# Changelog

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- RTOS-neutral profile-driven QEMU/GDB harness with dynamic GDB ports,
  session-local logs, persistent GDB connections, and actionable boot errors.
- FreeRTOS Phase 1 package and a B-L475E-IOT01A QEMU fixture built
  from STM32CubeL4 v1.18.2 and its pinned FreeRTOS V10.3.1 submodule.
- FreeRTOS Phase 2 version/config probes, DWARF task layouts, safe scheduler
  navigation, `freertos tasks/system`, `frt`, and task convenience functions.
- FreeRTOS fixture smoke tests covering boot readiness, debug type visibility,
  32-bit ABI, persistent commands, task enumeration, and system counters.

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
