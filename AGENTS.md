# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## Project

GDR is a GDB helper framework for debugging RTOS-based embedded firmware.
It runs **inside the GDB Python interpreter** via `source gdr.py` and provides
pretty-printers, convenience functions and RTOS-specific command trees.

## Architecture (layered)

```text
gdr.py                 entry point: parse args, load RTOS package, register
core/                  RTOS-agnostic core (bounded modules; adapters mirror its shape)
  gdb_bridge.py        GDB API wrappers, ELF/DWARF macro walk, error/output guards
  constants.py         shared traversal, string/table defaults, envelope schema
  formatting.py        pure value/table formatting + derived diagnostics helpers
  version.py           version parsing/decoding + probe registry and verdicts
  layout.py            generic StructLayout + accessors + pretty-printer registration
  help.py              structured help schema + terminal/Markdown renderers
  adapter.py           adapter protocol, tables, details and active session
  api.py               neutral verb layer + JSON envelope and chunked transport
  commands.py          shared collectors/renderers, watch set, convenience functions
  heapmap.py           heap grid, owner palette, PNG/inline frames, legend lettering
  unwind.py            bounded task unwind driver (seed, stack/PC validation, stop reasons)
  unwind_meta.py       bounded ELF sections, code symbols and binary encodings
  unwind_leaf.py       bounded instruction proofs for stack-neutral scalar leaf returns
  insn_arm.py          bounded ARM/A32 instruction decoder for code-evidence checks
  cpus.py              bounded Arm/RISC-V CPU attributes, conventions and ISA/ABI evidence
  stack_frame_parser.py validated task-frame profiles and common slot decoding
  insn_riscv.py        bounded RISC-V instruction decoding and code-range evidence
  insn_thumb.py        bounded Arm Thumb/Thumb-2 instruction and stream decoding
  unwind_cfi.py        bounded DWARF CFI tables and encoded-pointer decoding
  unwind_expr.py       bounded integer CFI expressions and guarded memory reads
  unwind_segments.py   typed stack segments, transition evidence and exception budgets
  unwind_arm.py        Arm M-profile frame recovery, Armv6-M/Armv7-M rules, SCB/vector reads and handler setup
  unwind_riscv.py      RISC-V M-mode trap frame recovery, IRQ stack bounds and handler setup
  unwind_ehabi.py      bounded ARM EHABI tables and virtual-register unwinding
rtthread/              RT-Thread 3.1.x/4.x adapter
  layout.py            ABI layouts + build_layouts(config) + version/probe policy
  navigation.py        symbols, object navigation, and heap snapshot
  stackpc.py           last PC from the saved switch frame (pure; slot table + proofs)
  backtrace.py         saved-context layouts and seed decoding (pure)
  backtrace_gdb.py     GDB-facing thread backtrace orchestration
  scheduler_switch.py  compiled M-profile transition profiles and stage classification
  scheduler_seed.py    owner-consistent seeds from classified M-profile stages
  stack_frame_profile.py compiled save-layout evidence and profile selection
  adapter.py           intermediate models, value converters, summaries and tables
  views.py             column contracts, detail-table cells, heap views/verdicts
  diagnostics.py       bounded raw-memory walks (IPC, heap) + `rtt check`
  commands.py          routing, completion, and structured help tree
freertos/              FreeRTOS adapter
  layout.py            config/DWARF probes, struct paths, version/probe policy
  navigation.py        scheduler lists + six-channel object discovery
  stackpc.py           last PC from the saved frame (pure; port evidence + proofs)
  backtrace.py         saved-context layouts and seed decoding (pure)
  backtrace_gdb.py     GDB-facing task backtrace orchestration
  scheduler_switch.py compiled M-profile transition profiles and stage classification
  scheduler_seed.py owner-consistent seeds from classified stages (basic/FPU geometry)
  stack_frame_profile.py compiled save-layout evidence and profile selection
  objects.py           per-kind decode: timers, event groups, stream buffers
  heap.py              heap_1..5 snapshot, canary, walks and cross-check verdicts
  adapter.py           task/object models, conversion, list tables, summaries
  views.py             column contracts, detail builders, merged detail tables
  diagnostics.py       bounded consistency checks + `frt check`
  commands.py          routing, completion, and structured help tree
vscode/                VSCode extension: a client of GDR's JSON envelope
  src/transport.ts     evaluate transport: $gdr_json, chunk reassembly, envelope
  src/probe.ts         probe payload -> init decision (never guesses a version)
  src/inject.ts        per-debug-adapter loader injection plan
  src/session.ts       first-stop lifecycle: Python check, load, probe, init
  src/runtime.ts       bundled runtime path + envelope-schema check
  src/snapshot.ts      snapshot envelope types/parse (per-kind tables, heap map),
                       cost args, freshness guard
  src/diff.ts          changed-cell baseline: values that moved since the last stop
  src/sourceFiles.ts   debug-info source path -> workspace file (pure resolver)
  src/memoryNav.ts     address -> mcu-debug memory-view URI (pure)
  src/panel.ts         panel controller: session tabs, stop-refresh, layout, heap map, nav
  src/webview/         host-rendered panel markup, webview client, style, state
    checkboxes.ts      two-level table/column checkbox tree (pure)
    layoutEdits.ts     drag-reorder and column-width rules (pure; client restates them)
    heapCanvas.ts      heap canvas grid, pointer-to-block lookup, hover readout (pure)
  src/extension.ts     activation, launch-config provider, debug-tracker wiring
  scripts/vendor-gdr.sh  generate resources/gdr from this repository's runtime
  media/               extension icons
  test/                node --test units, adapter-contract consistency, and a real QEMU/GDB session check
```

Key design principles (see `docs/architecture.md`):

- **Navigation belongs to helpers; display belongs to GDB.** Convenience
  functions return `gdb.Value`; commands only aggregate/tabulate.
- **No RTOS auto-detection.** User specifies `gdr init rtthread 4.0.5`.
  Kernel config features (SMP, heap type, IPC components) are probed at
  runtime by symbol presence, which is far more reliable than guessing the
  RTOS or parsing version strings.
- **Never drive target.** Target RTOS will keep halt when collect system runtime
  statuses. Inferior function calls (``foo()``) are prohibited. Identifier-only
  ``gdb.parse_and_eval`` is allowed only through ``core.gdb_bridge.eval_identifier``.
- **Layout is dataclass-driven, not YAML.** Kernel structs vary by *config*
  (SMP, heap manager, IPC toggles), not by version. A factory function
  `build_layouts(config)` assembles the right field set; small version
  deltas are handled with minimal conditional fields.
- **Coupling is explicit.** RTOS field layouts live in `<rtos>/layout.py`;
  RTOS symbols and traversal live in `<rtos>/navigation.py`. The `core/`
  package contains no RTOS-specific names or behavior.

## Setup

```bash
uv sync --group dev          # create .venv and install dev dependencies
uv run pre-commit install    # activate git hooks
```

## Commands

All commands run via `uv run` (auto-activates the `.venv`):

| Command | Purpose |
| --------- | --------- |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format (black-compatible) |
| `uv run ruff format --check .` | Verify formatting without writing |
| `uv run pytest tests/unit --cov` | Run unit tests and enforce core coverage |
| `python3 ci/test.py run RTOS TARGET VERSION [VARIANT]` | Prepare and run one integration lane |
| `cd vscode && npm run vendor` | Refresh the extension's bundled GDR runtime |
| `cd vscode && npm run check-types` | Type-check the extension |
| `cd vscode && npm test` | Extension units + real-session check (skips without fixtures) |

There is **no separate `black` tool**; `ruff format` is the drop-in
replacement and the only formatter used.

## Verification and CI

The public integration entry point is `python3 ci/test.py`. Commands, cache
initialization/linking, tool registration, containers, logs, and interface
layering are documented in [ci/README.md](ci/README.md). Coverage and target
profiles live in [docs/testing.md](docs/testing.md); measured build facts
belong to the [FreeRTOS](ci/freertos/README.md) and
[RT-Thread](ci/rt-thread/README.md) lane READMEs.

```bash
python3 ci/test.py list smoke
python3 ci/test.py run freertos mps2-an385 11.1.0 full -- -k backtrace
python3 ci/test.py matrix rtthread-rv64
python3 ci/test.py container run -- matrix smoke
```

Any work that consumes a config branch (SMP, heap_N, static allocation,
stream buffers, MPU, runtime stats, IRQ stacks, scheduling transitions, …)
must first have a fixture or snapshot that can falsify it. Target-specific
patches belong under `ci/rt-thread/patches/<target>/<version>/`; verify each
BSP/toolchain before sharing a patch.

### CI pipelines

| Verification axis | CNB pipeline | CLI matrix / command |
| --- | --- | --- |
| Python lint, hooks, unit coverage | `validate-py310`, `validate-py314` | `uv run pytest tests/unit --cov` |
| GDB 12 / Python 3.10 baseline | `gdb12-compat-a9` | `matrix gdb12` |
| RT-Thread Cortex-A9 | `rtthread-a9-target` | `matrix rtthread-a9` |
| RT-Thread RV64 | `rtthread-rv64-target` | `matrix rtthread-rv64` |
| RT-Thread port fixtures | `rtthread-ports-target` | `matrix rtthread-ports` |
| FreeRTOS config, version, ports and negative fixtures | `freertos-kernel-scope` | `matrix freertos` |
| VSCode build and checks | `extension` | See `vscode/README.md`. |

CNB definitions live in `.cnb.yml`; test pipelines use shared YAML anchors
for push and pull requests. Image publishers run only on push. GitHub
Actions uses the same CLI in `.github/workflows/ci.yml`.

## Documentation map

Each repository document serves one audience; a fact belongs in exactly one
place and is linked from the others, never rephrased into each.  When you
change behaviour (or the change gets written up), update the *one* file
that owns the fact:

| File | Audience | Owns | Must **not** carry | Granularity |
| --- | --- | --- | --- | --- |
| `README.md` / `README.zh-CN.md` | users | install, load, command index, output-decode notes | kernel internals, fixtures/CI, design rationale | each note ≤2 lines, mirror both languages |
| `AGENTS.md` | contributors/agents | file map (one line per file), setup/commands, CI lane table, conventions | per-module prose (point to architecture.md), lane internals (point to ci/*/README.md) | one line per file |
| `docs/architecture.md` | design readers | layering, module contracts, runtime flows, core mechanisms, RTOS capability comparison | VSCode panel/DOM detail, trial-and-error history, fixture build details | module-table cells ≤1-3 sentences |
| `docs/decisions.md` | design readers | architecture decisions and why they exist | mechanisms (→ architecture.md), limits (→ errata.md) | one decision per section, no change narration |
| `docs/testing.md` | contributors | closed-loop harness, QEMU profiles, version/variant matrices | fixture build steps (→ ci/*/README.md) | link lane READMEs, never copy |
| `docs/errata.md` | design readers | known limits and kernel differences by adapter | development rules (→ AGENTS.md) | one fact per bold item |
| `docs/gdb_python_reference.md` | developers | offline GDB Python API reference for bridge/stack work | project-specific claims | reference only, never edit by hand |
| `CHANGELOG.md` | users | user-visible behaviour changes | fixture/CI/lane/variant internals, implementation narrative, test-fix process notes | one line per change, ≤3 lines each |
| `ci/README.md` | contributors | integration CLI, shared cache, containers and orchestration layering | RTOS build mechanics | command-oriented |
| `ci/<rtos>/README.md` | lane maintainers | measured build facts, toolchains, cache, linker traps, unreachable variants | adapter design | none |
| `vscode/README.md` | extension users and lane maintainers | build/test/vendor commands, supported adapters, known limits | GDR internals, envelope design (link to architecture.md) | short sections, no per-file prose |

Rule of thumb: if a fact already exists in another file, add a link, never a
rewrite.  Process history ("this used to…", "formerly unit-tested…")
belongs in commit messages and run artifacts, not in repository docs.

## Conventions

- Python 3.10+, PEP8, type hints, Google-style docstrings.
- Files <= 1500 lines; split when approaching the limit.
- Relative imports within packages.
- Instruction-behaviour decisions in production logic must use decoder mnemonics and operands; direct matching of raw instruction encodings belongs only in architecture decoders and decoder tests.
- No external runtime dependencies (GDB Python API only). Dev tools
  (ruff/pytest/pytest-cov/pre-commit/pexpect) live in `[dependency-groups].dev`.
  `pexpect` drives the persistent GDB session in tests.
- Comment sparingly: no explanatory inline comments by default. Add `# NOTE:` only for a non-obvious contract the code cannot express (a GDB API trap, kernel ABI pitfall, or ordering invariant whose removal would invite a wrong simplification); `# TODO:` for planned follow-ups, `# FIXME:` for known defects. Docstrings state the contract (inputs, outputs, side effects, failure modes), never review justifications.
- Write current-state prose only: no `"used to"` / `"no longer"` change narration outside `CHANGELOG.md` (regression guards read `"Without X, Y happens"`), no self-justification (`"this is safe because…"`), no control-flow restatement. Preserve actor, condition, timing, `must`/`never` boundaries and failure consequences; cut adjectives and derivation transcripts. Prefer concrete technical terms over `surface`/`seam`/`shape`/`boundary`; one term per concept; bold only the clause that changes behaviour.
- When a layout-sensitive struct field changes in `rtthread/layout.py`,
  add or update the corresponding test assertion in `tests/`.

## Workflow

1. Read this file and `docs/architecture.md` before changing architecture.
2. Run `uv run ruff check .` and `uv run ruff format --check .` before
   committing; CI enforces both.
3. For layout changes, add/adjust a QEMU smoke test so the
   closed loop catches silent output drift.
