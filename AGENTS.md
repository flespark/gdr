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
  gdb_bridge.py        GDB Python API wrappers and error/output guards
  constants.py         shared traversal, string and table defaults
  formatting.py        pure optional/address/symbol/table formatting
  version.py           shared version parsing and numeric decoding
  layout.py            generic StructLayout + field/list accessors
  printers.py          wrapper-type pretty-printer registration
  adapter_api.py       adapter protocol, tables, details and active session
rtthread/              RT-Thread 3.1.x/4.x adapter
  layout.py            RT-Thread ABI field descriptions + build_layouts(config)
                       + detect_config() (symbol-presence probing)
  navigation.py        RT-Thread symbols and object navigation
  adapter.py           intermediate models, value converters, summaries and tables
  diagnostics.py       bounded raw-memory walks and consistency diagnostics
  version.py           RT-Thread version policy and target symbols
  commands.py          RT-Thread command tree (`rtt threads`, `rtt timers`, ...)
freertos/              FreeRTOS adapter
  layout.py            merged config probes, DWARF paths and capability metadata
  navigation.py        layout-driven scheduler-list traversal
  adapter.py           complete task model, conversion, summaries and tables
  version.py           FreeRTOS version policy and target symbols
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

## Verification and CI

### RT-Thread QEMU test

`bash ci/rtthread/run-qemu-matrix.sh` apply series arch/version triage patches
to RT-thread repo and build firmware for QEMU test.

QEMU fixture patches are grouped by platform first:
`ci/rt-thread/patches/cortex-a9/<version>/` and
`ci/rt-thread/patches/rv64/<version>/`. Do not share a patch across those
directories unless it applies cleanly to both BSP paths and toolchains.

The integration harness resolves fixture paths by default to the repo
sibling directory `<repo>/../fixture/<target>/<version>/rtthread_qemu.elf`
(plus `rtthread_qemu.bin` for RV64), or to explicit overrides when
`GDR_ELF_PATH` and `GDR_FIRMWARE_PATH` are set. Missing tools or fixture
artifacts are skipped per test via `pytest.skip` (see
`tests/support/qemu_harness.py`), so partial local caches stay usable.

```bash
# Run the Cortex-A9 matrix against a local fixture cache without rebuilding.
GDR_GDB=gdb-multiarch GDR_QEMU_TARGET=cortex-a9 \
  uv run pytest tests/integration/rtthread -v
```

RT-Thread 3.1.x is verified on the Cortex-A9 QEMU BSP only; the upstream
QEMU RV64 BSP starts at RT-Thread 4.0.4, so the RV64 matrix covers 4.0.4
through 4.1.1 (`bsp/qemu-virt64-riscv` from 4.1.1).

The RV64 target uses `qemu-system-riscv64 -machine virt` and boots
`rtthread.bin` as BIOS; GDB reads the separate `rtthread.elf`.

### FreeRTOS smoke test

`bash ci/freertos/run-qemu-matrix.sh` builds the locked STM32CubeL4 `v1.18.2`
B-L475E-IOT01A fixture (whose FreeRTOS submodule is commit
`5fe3a380e5eadb6ce0a5149725210c3fe70d1c15`) and runs it under QEMU:

```bash
bash ci/freertos/run-qemu-matrix.sh
```

The fixture uses the Cortex-M SysTick port (`portable/GCC/ARM_CM4F`) and QEMU
semihosting, not the board's unsupported LPTIM. Fixture sources live in
`ci/freertos/fixture/`; the build is defined in `ci/freertos/build-freertos.sh`.

### CI pipelines

CI runs on [CNB](https://cnb.cool/) (Cloud Native Build); pipelines are
defined in `.cnb.yml` (ruff + unit coverage on Python 3.10/3.14, the GDB 12
compatibility baseline, and Cortex-A9 and RV64 QEMU matrices). GitHub Actions
mirrors the validate jobs in `.github/workflows/ci.yml`. To reproduce the
current ARM and RV64 QEMU matrices locally in a Podman machine:

```bash
ci/validate-podman.sh
```

The script builds `ci/Dockerfile` for `linux/amd64` and uses the pinned xPack
toolchains. Start a Podman machine before running it.

## Conventions

- Python 3.10+, PEP8, type hints, Google-style docstrings.
- Files <= 1000 lines; split when approaching the limit.
- Relative imports within packages.
- No external runtime dependencies (GDB Python API only). Dev tools
  (ruff/pytest/pytest-cov/pre-commit/pexpect) live in `[dependency-groups].dev`.
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
