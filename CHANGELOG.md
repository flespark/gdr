# Changelog

<!-- markdownlint-configure-file { "MD024": { "siblings_only": true } } -->

All notable changes to GDR are documented in this file.

## [2026.05] - 2026-11-26

### Added

- `<rtos> backtrace [<task>] [--max-depth N]` (alias `bt`) and `gdr api backtrace`
  unwind saved or live task stacks (defaulting to the current halted PC), reporting
  frame PC, symbol/source, unwind method, completeness, and stop reason.
- Standalone `gdr backtrace [--max-depth N] [--json]` (and pre-init
  `gdr api backtrace`) unwinds bare-metal Arm and RISC-V targets without `gdr init`;
  it is refused once an RTOS adapter is active.
- Arm M-profile (`Armv6-M`–`Armv8-M Mainline` integer/FPU with `PSPLIM`) and
  Armv7-A backtraces cross nested IRQs, SysTick, NMI/DebugMonitor, precise faults,
  and PendSV/SVC/boot-MSP switches via DWARF CFI, EHABI, and proven leaf returns.
- RISC-V (`RV32`/`RV64`) backtraces unwind M-mode trap frames, precise synchronous
  faults, and scheduler switch stages across FreeRTOS 10.3.x–11.3.x and RT-Thread
  using `.debug_frame`/`.eh_frame`/`.eh_frame_hdr` CFI expressions and leaf proofs.
- `<rtos> watch backtrace [<task>] [--max-depth N]` and `<rtos> unwatch backtrace [<task>]`
  (alias `bt`) print task or current-PC backtraces automatically on every stop.
- VSCode panel adds expandable per-task backtraces from the `PC` column and floating
  detail popovers; GitHub and CNB releases now attach versioned `.vsix` packages.
- `<rtos> heap dump` adds `--cell-pixel`, `--width`, and `--row-bytes` options, block
  outlines, start/end address labels, `name: sizeB, start-end` legends, and automatic
  free-span folding.
- Terminal tables gain 3-mode elastic columns and shorten DWARF source paths relative
  to the inferred project/SDK root.

### Changed

- Object resolution across `<rtos> <object>`, `$gdr_task`/`$gdr_object`, `watch`, and
  `backtrace` uses one unified resolver accepting names, symbols, and hex/decimal
  addresses with explicit ambiguity errors.
- Backtrace JSON uses canonical `architecture` and `abi` fields (replacing `port` and
  duplicate `isa`); the envelope schema version is `3`.
- Out-of-GDB Python importers must use `core.*` and the reorganized `*/backtrace/`
  package paths; loading GDR via `source gdr.py` inside GDB is unchanged.
- FreeRTOS MPU task `Used` and `HighWater` measure the task stack rather than TCB
  context storage.
- Session symbol caches are shared across adapters and FreeRTOS handle scans refresh
  incrementally, reducing stop-time overhead.
- RT-Thread save-profile selection proves decoded register bitmaps, SP switch, and LWP
  header rather than fixed instruction fingerprints, expanding supported Armv7-A LWP
  releases while continuing to refuse pre-4.0 layouts.

### Fixed

- Task tables accurately decode the live `PC` of running tasks and saved `PC`s across
  Arm M-profile and FPU switch frames.
- FreeRTOS `heap_2` inspection accounts for V10.3.x heap base alignment, and adapters
  tolerate unreadable target fields without aborting table rendering.
- Shrink-wrapped Arm functions defer to per-PC DWARF CFI instead of mis-unwinding via
  EHABI, and RT-Thread switch-return CFI rows stay scoped to the outgoing owner.
- CFI unwinding honors extended offsets and CIE return-address columns independently of
  SP, discards undefined or unreadable registers, and refuses frame-pointer fallback on
  CFI parse failures.
- Backtrace symbols and source locations distinguish return addresses from exact
  seed/exception PCs, and Armv8-M saved-context selection rejects missing FPU/TrustZone
  config macros instead of assuming them disabled.

## [2026.04] - 2026-10-13

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
  `gdr api check` reports the system verdicts as structured findings.
- Detail blocks carry a `sections` view of their consistency verdicts with
  per-row `ok`/`fail`/`skipped` severity; the flat text output is unchanged.
- `gdr reset` and `gdr init --force` replace the active adapter without
  restarting GDB, so switching target or version no longer costs a session.
- A versioned JSON API for tooling (the IDE plugin): `gdr api <verb>` prints
  one envelope and `$gdr_json` / `$gdr_json_chunk` return it through GDB
  evaluate, with a chunked fallback for payloads larger than one result.
  `gdr init --json` reports the init outcome as one structured line.
- `<rtos> heap dump [path]` draws the heap occupancy bitmap: grey for free
  blocks and one colour per owner. Inline in iTerm2 or Kitty; otherwise a PNG
  (`gdr-heap-<rtos>-gen<N>.png`). The panel's heap card draws the same map with a
  hover readout.
- `rtt check [scope] [target]` runs explicit consistency checks (thread stack/priority,
  timer flag-vs-list membership, current-thread/tick/registry types, heap chain) with
  `ok`/`fail`/`skipped` verdicts and object addresses; unavailable fields report the
  missing symbol, field or config macro instead of guessing. `gdr api check` now
  covers the RT-Thread system verdicts as well as the heap ones.
- The check command is singular everywhere: `frt checks` is now `frt check` and
  `gdr api checks` is now `gdr api check` (both old spellings stay as aliases).
- The `rtt timers` table gains a `Src` column recording registry and/or active-list
  evidence per timer, so a dormant timer and a detached-but-linked one are told apart.
- `<rtos> watch [<kind> | <object> <target>]` prints watched kinds (list table)
  or singletons (detail block) on every target stop, reusing the same collectors
  as the commands; `<rtos> unwatch` drops them and bare `<rtos> watch` lists the
  set. Like GDB `display`, not a watchpoint: it never stops the target. Watches
  are session state dropped by `gdr reset`.
- Singular object details accept a name, an explicit address or a symbol:
  `<rtos> <object> <name-or-address>` (and the `detail` API verb plus
  `$gdr_task`/`$gdr_object`) resolve a name first, then match an address
  literal or symbol against live objects, so duplicate-named or nameless
  objects stay reachable by their `Addr` column.

### Changed

- `gdr init --json` reports `verified` and `markers`, adds a `warning` when the
  target could not confirm the adapter, and reports refusals as a coded
  `error` object (the same shape the API envelope uses) instead of a bare
  message string.
- FreeRTOS `HighWater` now matches RT-Thread's historical maximum used bytes,
  not remaining bytes; unavailable without retained stack bounds and prefill.
- FreeRTOS normal views show concrete check problems instead of verified/n/a
  counts; `frt check [scope] [name]` exposes all verdicts with object addresses.
- Memory sizes are unified to bytes: key/value sizes print with a `B` suffix,
  and table columns state bytes in their descriptions rather than in headers.
- The panel's system summary is one header row and one value row, the task
  table's shape. Kernel version is gone from `rtt system` and the panel.
- `rtt heap` drops the Holes line and the per-thread occupancy table; its
  verdicts move into the table as diagnostic columns. The Checks card is gone
  from the panel. `gdr api check` stays.
- A new `PC` column after `BasePrio` shows the thread's last PC, proven from
  its saved switch frame or `N/A`.

### Fixed

- FreeRTOS stack size/usage include the final recorded StackType_t; HighWater
  includes saved-SP reservations so it never falls below Used, even when initial
  context slots retain their fill bytes.
- Redundant RT-Thread object Type and timer KernelTick columns are omitted from
  merged tables; meaningful timer classes and FreeRTOS subtypes remain available.
- Snapshots taken at an RT-Thread reset-vector stop no longer fail on NULL
  timer-list links; startup lists stop safely instead of producing fake objects
  at wrapped addresses. Normal collection works after kernel startup.
- Heap maps preserve every block at a minimum of 6×6 pixels without trailing
  background; exported PNGs include owner/free colour legends and byte totals.
- RT-Thread heap walk counts used blocks by their full span, matching the
  kernel counters, so `HeapAccounting` no longer reports a false mismatch.
  `rtt heap` Holes states its sizes in bytes; the panel's heap table is titled
  by what it lists ("Heap used by thread" or "Free list").
- The VSCode extension checks Python before `source gdr.py` at launch. Without
  Python the session still starts and the panel shows why. The first stop does
  not check Python or source again. The source path is unquoted so GDB 16
  accepts it.
- Version detection reads a kernel's version macros from the loaded image's
  debug strings instead of GDB's compilation-unit scope, so it keeps working when
  the build tree is gone (a flashed release image) or when the halted PC sits in
  a unit that has not included the version header yet.
- RT-Thread `Scheduler state` reads the scheduler lock and interrupt nest
  counters: `Unlocked`, `Locked (nest N)`, or `In ISR (nest N)`. It was always
  `N/A`; the row is omitted when no counter can be read.

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

- `heap` and `objects` render through the neutral `core.commands` renderers
  (`HeapReport` + `object_summary_table` adapter protocol), so `rtt` and
  `frt` share one code path and cannot drift.

### Fixed

- `gdr init` no longer kills the GDB session on policy failures (unknown
  RTOS name, invalid or unsupported version, declared/target version
  mismatch): it warns and returns to the prompt with no adapter registered.
- `rtt` and `frt` heap/system output uses a single `N/A` sentinel.

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
