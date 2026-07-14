# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## Project

GDR is a GDB helper framework for debugging RTOS-based embedded firmware.
It runs **inside the GDB Python interpreter** via `source gdr.py` and provides
pretty-printers, convenience functions and aggregate commands.

## Architecture (layered)

```
gdr.py                 entry point: parse args, load RTOS package, register
gdr/                   RTOS-agnostic core
  gdb_bridge.py        GDB Python API wrappers (register, table, error guard)
  layout.py            generic StructLayout dataclass + field/list accessors
  printers.py          wrapper-type pretty-printer registration
  kernel.py            object navigation (global symbol -> object tree)
  abstractions.py      minimal ABCs (Thread/Semaphore/Mutex/Timer/Queue)
rtthread/              RT-Thread v4.0.x adapter
  layout.py            dataclass field descriptions + build_layouts(config)
                       + detect_config() (symbol-presence probing)
  adapter.py           value→dataclass converters + gdb.Function convenience
                       functions ($gdr_thread, $gdr_threads, $gdr_object)
  commands.py          5 aggregate commands (threads/semaphores/timers/objects/system)
tests/                 QEMU closed-loop verification (pexpect-driven)
  conftest.py          QemuSession + GdbSession (persistent GDB via pexpect)
  test_commands.py     aggregate command output assertions
  test_functions.py    convenience function return-value assertions
  test_printers.py     pretty-printer fold-output assertions
```

Key design principles (see `docs/architecture.md`):
- **Navigation belongs to helpers; display belongs to GDB.** Convenience
  functions return `gdb.Value`; commands only aggregate/tabulate.
- **No RTOS auto-detection.** User specifies `--rtos rtthread --version 4.0`.
  Kernel config features (SMP, heap type, IPC components) are probed at
  runtime by symbol presence, which is far more reliable than guessing the
  RTOS or parsing version strings.
- **Layout is dataclass-driven, not YAML.** Kernel structs vary by *config*
  (SMP, heap manager, IPC toggles), not by version. A factory function
  `build_layouts(config)` assembles the right field set; small version
  deltas are handled with minimal conditional fields.
- **Coupling is explicit.** Layout-sensitive knowledge lives only in
  `rtthread/layout.py`. When RT-Thread structs change, that is the single
  file to review.

## Setup

```bash
uv sync --group dev          # create .venv and install dev dependencies
uv run pre-commit install    # activate git hooks
```

## Commands

All commands run via `uv run` (auto-activates the `.venv`):

| Command | Purpose |
|---------|---------|
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format (black-compatible) |
| `uv run ruff format --check .` | Verify formatting without writing |
| `uv run pytest tests/ -v` | Run QEMU closed-loop tests |

There is **no separate `black` tool**; `ruff format` is the drop-in
replacement and the only formatter used.

## Conventions

- Python 3.10+, PEP8, type hints, Google-style docstrings.
- Files <= 500 lines; split when approaching the limit.
- Relative imports within packages.
- No external runtime dependencies (GDB Python API only). Dev tools
  (ruff/pytest/pre-commit/pexpect) live in `[dependency-groups].dev`.
  `pexpect` drives the persistent GDB session in tests.
- Add `# Reason:` inline comments for non-obvious *why* decisions.
- When a layout-sensitive struct field changes in `rtthread/layout.py`,
  add or update the corresponding test assertion in `tests/`.

## Workflow

1. Read this file and `docs/architecture.md` before changing architecture.
2. Run `uv run ruff check .` and `uv run ruff format --check .` before
   committing; CI enforces both.
3. For layout changes, add/adjust a QEMU smoke test so the
   closed loop catches silent output drift.
