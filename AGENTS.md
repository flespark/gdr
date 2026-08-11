# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## Project

GDR is a GDB helper framework for debugging RTOS-based embedded firmware.
It runs **inside the GDB Python interpreter** via `source gdr.py` and provides
pretty-printers, convenience functions and RTOS-specific command trees.

## Architecture (layered)

```text
gdr.py                 entry point: parse args, load RTOS package, register
gdr/                   RTOS-agnostic core
  gdb_bridge.py        GDB Python API wrappers (register, table, error guard)
  layout.py            generic StructLayout dataclass + field/list accessors
  printers.py          wrapper-type pretty-printer registration
  abstractions.py      neutral table-output dataclasses
rtthread/              RT-Thread v4.x adapter
  layout.py            dataclass field descriptions + build_layouts(config)
                       + detect_config() (symbol-presence probing)
  navigation.py        RT-Thread symbols and object navigation
  adapter.py           value→dataclass converters + semantic adapter contract
  commands.py          RT-Thread command tree (`rtt threads`, `rtt timers`, ...)
freertos/              FreeRTOS adapter
  commands.py          FreeRTOS command tree (`frt tasks`, `frt system`)
gdr/                   semantic command/function core ($gdr_task, $gdr_tasks,
                       $gdr_object; internal renderers, not gdr subcommands)
tests/
  unit/                hardware-independent core and adapter tests
    core/              RTOS-neutral rendering, layout, registration, bootstrap
    rtthread/          RT-Thread layout, navigation, version, adapter tests
    freertos/          FreeRTOS adapter tests
  integration/         QEMU/GDB closed-loop tests and fixtures
    rtthread/          command, function, and pretty-printer assertions
    freertos/          FreeRTOS boot and command assertions
  support/             shared QEMU harness and fixture expectation profiles
```

Key design principles (see `docs/architecture.md`):

- **Navigation belongs to helpers; display belongs to GDB.** Convenience
  functions return `gdb.Value`; commands only aggregate/tabulate.
- **No RTOS auto-detection.** User specifies `gdr init rtthread 4.0.5`.
  Kernel config features (SMP, heap type, IPC components) are probed at
  runtime by symbol presence, which is far more reliable than guessing the
  RTOS or parsing version strings.
- **Layout is dataclass-driven, not YAML.** Kernel structs vary by *config*
  (SMP, heap manager, IPC toggles), not by version. A factory function
  `build_layouts(config)` assembles the right field set; small version
  deltas are handled with minimal conditional fields.
- **Coupling is explicit.** RT-Thread field layouts live in
  `rtthread/layout.py`; RT-Thread symbols and traversal live in
  `rtthread/navigation.py`. The `gdr/` core contains no RT-Thread-specific
  names or behavior.

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
| `uv run pytest tests/unit --cov` | Run unit tests and enforce core coverage |
| `uv run pytest tests/integration -v` | Run QEMU tests when fixtures exist |

There is **no separate `black` tool**; `ruff format` is the drop-in
replacement and the only formatter used.

## Conventions

- Python 3.10+, PEP8, type hints, Google-style docstrings.
- Files <= 500 lines; split when approaching the limit.
- Relative imports within packages.
- No external runtime dependencies (GDB Python API only). Dev tools
  (ruff/pytest/pytest-cov/pre-commit/pexpect) live in `[dependency-groups].dev`.
  `pexpect` drives the persistent GDB session in tests.
- Add `# Reason:` inline comments for non-obvious *why* decisions.
- When a layout-sensitive struct field changes in `rtthread/layout.py`,
  add or update the corresponding test assertion in `tests/`.
- QEMU fixture patches are grouped by platform first:
  `ci/rt-thread/patches/cortex-a9/<version>/` and
  `ci/rt-thread/patches/rv64/<version>/`. Do not share a patch across those
  directories unless it applies cleanly to both BSP paths and toolchains.
- RV64 uses `qemu-system-riscv64 -machine virt` and boots `rtthread.bin` as
  BIOS; GDB reads the separate `rtthread.elf`. It is supported from RT-Thread
  v4.0.4 onward; v4.1.1 renames its BSP to `qemu-virt64-riscv`.

## Workflow

1. Read this file and `docs/architecture.md` before changing architecture.
2. Run `uv run ruff check .` and `uv run ruff format --check .` before
   committing; CI enforces both.
3. For layout changes, add/adjust a QEMU smoke test so the
   closed loop catches silent output drift.
