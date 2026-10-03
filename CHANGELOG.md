# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [Unreleased]

### Added

- `gdr probe [--json]` reports which adapter a target matches with the evidence
  behind the claim (matched symbols, version source, layout fingerprint) and a
  confidence of `exact`, `range` or `undetected`; `gdr init <rtos> auto` uses it
  only when a single exact answer exists. `$gdr_json` now exists from
  `source gdr.py`, so a client can ask before initializing.
- A VSCode extension under `vscode/` consumes the JSON API: it loads GDR into
  the session's own GDB, probes the target, initializes the adapter the user
  confirms, and states precisely why a session cannot host GDR.
- `gdr api snapshot --no-heap` drops the heap walk from a snapshot, so a
  client's stop-time refresh can skip the only full-memory traversal among the
  panel verbs.
- `gdr init` refuses to register an adapter when the target matches no RTOS
  symbol, instead of answering every later read with empty tables;
  `gdr init <rtos> <version> --allow-unverified` overrides that for a kernel
  whose symbols were deliberately modified or stripped. An unverified
  registration is recorded on the session and repeated in every API envelope's
  `warnings`, so a client shows the caveat rather than trusting the reads.
- The extension gains an RTOS panel in the bottom panel area (one tab per debug
  session): task/object/heap/check cards rendered from the snapshot envelope,
  stop-driven refresh with a generation guard, configurable heap collection
  (`gdr.refreshHeaps`), JSON snapshot recording for bug reports
  (`gdr.recordSnapshotsDir`), a transport choice for very large payloads
  (`gdr.snapshotTransport`), and address navigation into the mcu-debug memory
  viewer.
- The extension checks GDB's Python at launch instead of at the first stop: a
  generated loader file records `present`/`absent`, so a GDB built without
  Python no longer aborts the debug launch, and the panel names the `-py3`
  binary to use instead.
- Typed presentation contract for tooling: every list table declares its
  columns (`ColumnSpec`), carries machine values beside the rendered text and
  names its identity column, so a client can sort, right-align and key rows
  without re-parsing `N/A`/`0x` strings. `gdr api capabilities` reports each
  kind's columns and the configuration that would enable a missing one, and
  `gdr api checks` reports the system verdicts as structured findings.
- Detail blocks carry a `sections` view of their consistency verdicts with
  per-row `ok`/`fail`/`skipped` severity; the flat text output is unchanged.
- `gdr reset` and `gdr init --force` replace the active adapter without
  restarting GDB, so switching target or version no longer costs a session.
- A versioned JSON API for tooling (the IDE plugin): `gdr api <verb>` prints
  one envelope and `$gdr_json` / `$gdr_json_chunk` return it through GDB
  evaluate, with a chunked fallback for payloads larger than one result.
  `gdr init --json` reports the init outcome as one structured line.

### Changed

- `gdr init --json` reports `verified` and `markers`, adds a `warning` when the
  target could not confirm the adapter, and reports refusals as a coded
  `error` object (the same shape the API envelope uses) instead of a bare
  message string.

### Fixed

- The VSCode extension checks Python before `source gdr.py` at launch. Without
  Python the session still starts and the panel shows why. The first stop does
  not check Python or source again. The source path is unquoted so GDB 16
  accepts it.
- Version detection reads a kernel's version macros from the loaded image's
  debug strings instead of GDB's compilation-unit scope, so it keeps working when
  the build tree is gone (a flashed release image) or when the halted PC sits in
  a unit that has not included the version header yet.

## [2026.03] - 2026-09-22

### Added

- Git-style structured help trees for `gdr`, `rtt` and `frt`: every list,
  detail and heap topic documents output fields, usage tips, configuration
  dependencies and implementation limits; convenience functions and active
  layout-derived pretty-printers are also discoverable. The same dataclasses
  render terminal help and MkDocs-ready Markdown.
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
