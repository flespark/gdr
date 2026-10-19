# Decisions

The architecture decisions behind GDR's layering. Each entry states the rule
and why it exists; the mechanisms themselves are described in
`architecture.md`, and current limitations in `errata.md`.

## Explicit initialization, never auto-detection

Users specify the RTOS and exact version (`gdr init rtthread 4.0.5`).
Probing a version string from symbols and matching struct patterns failed on
attach and across remote configurations, so GDR never selects an adapter on
the user's behalf.

`gdr probe` and `gdr init` read the same evidence in the same order — the
adapter's presence markers, then the version constants — and differ only in
what they do with a missing answer: `probe` reports `undetected` and leaves
the decision to the user, while `init` refuses to register. An adapter
registered against a target with no matching symbol answers every read with
empty tables, which looks like a broken kernel rather than a wrong adapter.

Two deliberate asymmetries keep real workflows working:

- **Markers present but no exported version** is *accepted* with a note: the
  user asserted the version, which is what a client needs after an
  `undetected` probe.
- **`--allow-unverified`** registers anyway, for a kernel whose symbols were
  deliberately modified or stripped. The override is not silent: the verdict's
  note is stored on the session and prepended to every later envelope's
  `warnings`, so a client keeps it available without silently trusting the
  reads.

## Kernel configuration is probed, not specified

RTOS kernels vary by *configuration* far more than by version. For RT-Thread,
`RT_USING_SMP` adds `oncpu` to `rt_thread`, the heap manager changes the heap
structures entirely, and IPC components may be absent. Probing these by symbol
presence is reliable and cheap, and spares users from reciting their `.config`.
Probing falls back to safe defaults with a warning when a symbol is ambiguous.
Symbol presence uses DWARF/ELF lookup, never a GDB expression that could call
into the target.

## The target is never driven

GDR inspects a halted RTOS. `gdb.parse_and_eval("foo()")` resumes the core
until the dummy frame returns, so SysTick and pending peripheral ISRs can
mutate ticks, thread state, and IPC objects. Collection therefore uses only
symbol lookup for identifiers, `gdb.Value` field/index access for members and
arrays, and identifier-only `eval_identifier` so GDB expands macros such as
packed version constants on the host. Target helpers such as `rt_tick_get()`
are never called; SMP current-CPU identity comes from the halted register file
or GDB's selected thread.

## Layouts are adapter-owned dataclasses, not YAML schemas

Structs vary by **config**, not version. A YAML schema would need conditional
fields and overlays, turning the loader into a mini interpreter; per-version
files would be 99% duplicate. Python dataclasses handle config-conditional
fields naturally via factory functions (`build_layouts(config)`), with
minimal conditional fields for small version deltas.

The adapter owns concrete type names, field paths, display labels, state
encodings, target symbols, and object-registry traversal. The core only
consumes generic layout metadata, so another RTOS can use different wrappers
and object types without changing `core/`.

## Coupling is explicit and localised

All RT-Thread coupling lives under `rtthread/` (ABI field paths and state
encodings in `layout.py`, symbols and raw traversal in `navigation.py`,
presentation models and tables in `adapter.py`, bounded raw-memory walks in
`diagnostics.py`). `gdr.py` is the composition root; modules inside `core/`
never import or identify an RTOS. An adapter depends only on `core/`
contracts and its own package, never on another adapter. Presentation flows
inward as `adapter -> ObjectTable/ObjectDetail -> core renderer`; the core
does not inspect RTOS model fields.

## Commands aggregate, functions navigate

Commands provide the multi-object presentation GDB expressions cannot
produce: tables, trees, and derived summaries. Convenience functions may
navigate a collection but return raw `gdb.Value`, leaving element inspection
to native GDB expressions. This keeps the command set small and avoids
commands silently breaking when a field is renamed.

## One machine-readable contract from the same collectors

Text tables cannot be consumed by an IDE, so the client contract is a
versioned JSON envelope reached through `$gdr_json` (or base64 frames through
`$gdr_json_chunk`, frozen from a single envelope so a moving target cannot
reassemble two snapshots). `core/api.py` routes every verb through the same
collectors the text renderers print, so a verb result and its command output
cannot drift apart. `SCHEMA_VERSION` is bumped only on a breaking envelope
change and is independent of the release version.

## A thread's last PC is proven, never guessed

No supported RTOS keeps a program counter in its TCB, so the
PC column is read from the thread's saved switch frame and admitted only with
proof. For RT-Thread the frame slot is keyed by version and pointer width,
and a value must equal the thread's entry, follow a call to
`rt_hw_context_switch`, or name a symbolised code address. For FreeRTOS the
slot is keyed by port evidence (build attributes and port symbols, since
Cortex-M3/M33 share `PendSV_Handler`), and a frame must name code with the
Thumb bit set (Cortex-M) or a symbol (RISC-V). Anything unproven renders `N/A`.
