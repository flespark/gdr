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
  derive.py            RTOS-neutral derived-value helpers (wrap-safe expiry,
                       fill-byte watermark, waiter cell) shared by adapters
rtthread/              RT-Thread 3.1.x/4.x adapter
  layout.py            RT-Thread ABI field descriptions + build_layouts(config)
                       + detect_config() (symbol-presence probing)
  navigation.py        RT-Thread symbols, object navigation, and heap snapshot
  adapter.py           intermediate models, value converters, summaries and tables
  diagnostics.py       bounded raw-memory walks (IPC, heap) and consistency diagnostics
  version.py           RT-Thread version policy and target symbols
  commands.py          RT-Thread command tree (`rtt threads`, `rtt heap`, ...)
freertos/              FreeRTOS adapter
  layout.py            config/DWARF probes, ABI struct paths, symbol & kind tables
  navigation.py        scheduler-list traversal, per-TCB state, six-channel object discovery
  timers.py / events.py / streams.py / heap.py
                       per-object-kind decode (timer daemon queue, event-group bits,
                       stream-buffer geometry, system-heap snapshot)
  adapter.py           task/object models, conversion, list tables, summaries
  details.py / diagnostics.py
                       vertical detail builders and bounded raw-memory consistency checks
  version.py           FreeRTOS version policy and target symbols
  commands.py          FreeRTOS command tree (`frt tasks/.../heap`, 7 plural + 7 singular + 6 aliases)
gdr/                   semantic command/function core ($gdr_task, $gdr_tasks,
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
  ``gdb.parse_and_eval`` is allowed only through ``gdr.gdb_bridge.eval_identifier``.
- **Layout is dataclass-driven, not YAML.** Kernel structs vary by *config*
  (SMP, heap manager, IPC toggles), not by version. A factory function
  `build_layouts(config)` assembles the right field set; small version
  deltas are handled with minimal conditional fields.
- **Coupling is explicit.** RTOS field layouts live in `<rtos>/layout.py`;
  RTOS symbols and traversal live in `<rtos>/navigation.py`. The `gdr/` core
  contains no RTOS-specific names or behavior.

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
| `uv run pytest tests/integration -v` | Run QEMU tests when fixtures exist |

There is **no separate `black` tool**; `ruff format` is the drop-in
replacement and the only formatter used.

## Verification and CI

### RT-Thread QEMU test

`bash ci/rt-thread/run-qemu-matrix.sh` apply series arch/version triage patches
to RT-thread repo and build firmware for QEMU test.

QEMU fixture patches are grouped by platform first:
`ci/rt-thread/patches/cortex-a9/<version>/` and
`ci/rt-thread/patches/rv64/<version>/`. Do not share a patch across those
directories unless it applies cleanly to both BSP paths and toolchains.

The integration harness resolves fixture paths from
`RT_THREAD_FIXTURE_CACHE/<target>/<version>/rtthread.elf` (plus
`rtthread.bin` for RV64). The default cache root is the CNB host path
`/workspace/fixture/rtthread`. An explicit `GDR_ELF_PATH` still wins; on
RV64 the QEMU boot image is always its `.bin` sibling. Env parsing lives in
`tests/support/loader.py`; missing tools or fixture artifacts are skipped
per test via `pytest.skip` (see `tests/support/qemu_harness.py`), so
partial local caches stay usable.

```bash
# Run the Cortex-A9 matrix against the default CNB fixture cache.
GDR_GDB=gdb-multiarch bash ci/rt-thread/run-qemu-matrix.sh cortex-a9

# Or point at another cache root / a single pytest session.
RT_THREAD_FIXTURE_CACHE=/path/to/cache GDR_GDB=gdb-multiarch \
  GDR_QEMU_TARGET=cortex-a9 GDR_VERSION=4.0.5 \
  uv run pytest tests/integration/rtthread -v
```

RT-Thread 3.1.x is verified on the Cortex-A9 QEMU BSP only; the upstream
QEMU RV64 BSP starts at RT-Thread 4.0.4, so the RV64 matrix covers 4.0.4
through 4.1.1 (`bsp/qemu-virt64-riscv` from 4.1.1).

The RV64 target uses `qemu-system-riscv64 -machine virt` and boots
`rtthread.bin` as BIOS; GDB reads the separate `rtthread.elf`.

### FreeRTOS smoke test

FreeRTOS verification has two live lanes, one builder each, plus the static
`snapshot` variant built by the kernel-direct builder; all install into one
fixture cache (details in `ci/freertos/README.md`):

- **CubeL4 live (`b-l475e-iot01a`):** `ci/freertos/build-fixture-cubel4.sh`
  compiles the shared fixture against the FreeRTOS submodule bundled in
  STM32CubeL4 `v1.18.2` (commit
  `5fe3a380e5eadb6ce0a5149725210c3fe70d1c15`), so it is pinned to kernel
  `10.3.1` and carries the **config variant** matrix.
- **Kernel-direct live (`mps2-an385`, `mps2-an521`, `qemu-virt-rv64`):**
  `ci/freertos/build-fixture-kernel.sh` clones `FreeRTOS-Kernel` at a tag and
  links a port directory chosen by target, so it carries the **kernel version**
  matrix. `mps2-an385` links `portable/GCC/ARM_CM3` (single core).
  `mps2-an521` links `portable/GCC/ARM_CM33_NTZ/non_secure` and hosts two
  mutually exclusive single-board variants: the **dual-core SMP** lane (QEMU
  models that board as SSE-200 with two Cortex-M33, ARMv8-M SMP exists only
  from `V11.3.1` -- `portVALIDATED_FOR_SMP` is 0 in every earlier tag -- so it
  is pinned to `11.3.1` with the `smp` config variant) and the **single-core
  `mpu`** variant (`configENABLE_MPU 1`, MPU wrappers v2) that populates
  `xKernelObjectPool`. Unlike the CM3 port, CM33 needs `portasm.c` compiled
  alongside `port.c`; the `mpu` variant additionally links
  `portable/Common/mpu_wrappers_v2.c` and `mpu_wrappers_v2_asm.c`.
  `qemu-virt-rv64` links `portable/GCC/RISC-V` (rv64imac, `portASM.S` +
  `chip_specific_extensions/RISCV_MTIME_CLINT_no_extensions`) and boots on
  QEMU `-machine virt` with the SiFive CLINT tick; it is the 64-bit lane
  (64-bit pointers, and the RISC-V port always uses 64-bit ticks). The
  kernel-direct compiler follows the target (`riscv-none-elf-` on
  `qemu-virt-rv64`, `arm-none-eabi-` elsewhere).
- **Static snapshot** (kernel-direct variant `snapshot`, cell
  `mps2-an521/11.1.0`): `ci/freertos/build-fixture-kernel.sh` compiles a
  file-only Cortex-M33 ELF pair from `fixture/config/snapshot/` whose `.data`
  holds pre-initialized SMP scheduler structures plus the diagnostic
  negatives a healthy kernel cannot produce (corrupt lists, a timer on the
  overflow list, a heap whose free-list/linear/counter triple disagrees) —
  the data-corruption negative-testing arm of the matrix. A second, heap-only
  ELF (`snapshot_heap.c` -> `snapshot_heap.elf`) carries the
  free-list-member-with-allocated-bit case, which cannot share one heap
  symbol set with the mismatch case. `tests/integration/freertos/test_snapshot.py`
  loads them with `file` only (no QEMU) via
  `tests/support/freertos_elf_harness.py`; a cached snapshot ELF older than
  its sources is rebuilt, like the live lanes.

`bash ci/freertos/run-qemu-matrix.sh [<target>] [<version>] [<variant>...]`
drives the live lanes and the file-only `snapshot` variant.

```bash
bash ci/freertos/run-qemu-matrix.sh b-l475e-iot01a 10.3.1 base full static-dynamic
bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.1.0 snapshot   # file-only ELF
bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.3.1 smp        # dual-core SMP
bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.3.1 mpu        # MPU wrappers v2
bash ci/freertos/run-qemu-matrix.sh qemu-virt-rv64 11.1.0 base   # 64-bit RISC-V
# The GDR_GDB used for the closed loop must match the lane's architecture
# (riscv-none-elf-gdb-py3 for qemu-virt-rv64, arm-none-eabi-gdb-py3 elsewhere).

# Point the fixture cache elsewhere. A cached fixture is reused only while it is
# newer than the fixture sources; a missing or stale one is rebuilt and installed.
FREERTOS_FIXTURE_CACHE=/path/to/cache \
  bash ci/freertos/run-qemu-matrix.sh b-l475e-iot01a 10.3.1 base
```

Cache layout is `<cache>/<target>/<version>/<variant>/freertos.elf` rooted at
`FREERTOS_FIXTURE_CACHE` (the snapshot cell `mps2-an521/11.1.0/snapshot/`
also holds `snapshot_heap.elf`; default `~/Project/gdr-fixture/freertos`).
`GDR_FIXTURE_VARIANT` selects the
variant, `GDR_FORCE_BUILD=1` forces a rebuild, and a missing artifact is
`pytest.skip` so partial local caches stay usable. The B-L475E fixture uses the
Cortex-M SysTick port (`portable/GCC/ARM_CM4F`) and QEMU semihosting, not the
board's unsupported LPTIM. Shared fixture sources live in
`ci/freertos/fixture/` (`config/<variant>/`, `board/<board>/`, `main.c`).
Board code owns the platform details a shared `main.c` must not carry: the
`mps2-an521` board directory holds the CPU1 entry point and the
`configWAKE_SECONDARY_CORES` implementation (write `INITSVTOR1`, then clear the
`CPUWAIT` bit -- that order matters, because clearing the bit warm-resets CPU1
from whatever `INITSVTOR1` holds at that moment).

That fixture has one documented limitation: it does not override the port's weak
`vInterruptCore`, so cross-core yield requests are not delivered as interrupts
and a task caught mid-yield can render as `Running(yielding)`. Every
ground-truth waiter task is pinned to core 0 so no shared assertion depends on a
cross-core yield. See `ci/freertos/README.md` for why the MHU doorbell is not
wired.

`GDR_GDB` must name a GDB with embedded Python: xPack's `arm-none-eabi-gdb`
has none, its sibling `arm-none-eabi-gdb-py3` does. Probe with
`"$GDR_GDB" --nx --quiet --batch --ex 'python print("ok")'` instead of trusting
the binary's name.

Any work that consumes a config branch (SMP, heap_N, static allocation,
stream buffers, MPU, runtime stats, …) must first have a fixture or
snapshot that can falsify it.

### CI pipelines

CI runs on [CNB](https://cnb.cool/) (Cloud Native Build); pipelines are
defined in `.cnb.yml`, one per verification axis and named `<rtos>-<axis>`:
ruff + unit coverage on Python 3.10/3.14, the GDB 12 compatibility baseline,
RT-Thread split by target (`rtthread-a9-target` / `rtthread-rv64-target`), and
FreeRTOS split by what each lane can falsify — `freertos-config-scope`
(kernel 10.3.1, 14 config variants) and `freertos-version-scope` (kernel
version sweep, the dual-core SMP and single-core MPU lanes on mps2-an521,
the 64-bit RISC-V lane, and the static snapshot for data-corruption negative
testing). The FreeRTOS split matches the two fixture builders, so each lane
mounts only the sources it builds from. Every test pipeline is defined once as a YAML anchor
and referenced from both `push:` and `pull_request:`; the two event lists differ
only in the two image publishers, which are push-only so a PR never moves a
shared tag.
GitHub Actions mirrors the validate jobs in `.github/workflows/ci.yml`. To
reproduce the current ARM and RV64 QEMU matrices locally in a Podman machine:

```bash
ci/validate-podman.sh
```

The script builds `ci/Dockerfile` for `linux/amd64` and uses the pinned xPack
toolchains. Start a Podman machine before running it.

## Documentation map

Each repository document serves one audience; a fact belongs in exactly one
place and is linked from the others, never rephrased into each.  When you
change behaviour (or the DtoD clerk writes up a phase), update the *one* file
that owns the fact:

| File | Audience | Owns | Must **not** carry | Granularity |
| --- | --- | --- | --- | --- |
| `README.md` / `README.zh-CN.md` | users | install, load, command index, output-decode notes | kernel internals, fixtures/CI, design rationale | each note ≤2 lines, mirror both languages |
| `AGENTS.md` | contributors/agents | file map (one line per file), setup/commands, CI lane table, conventions | per-module prose (point to architecture.md), lane internals (point to ci/*/README.md) | one line per file |
| `docs/architecture.md` | design readers | layering, key decisions, invariants, known constraints | trial-and-error history, fixture build details, per-lane evidence | module-table cells ≤1-3 sentences |
| `CHANGELOG.md` | users | user-visible behaviour changes | fixture/CI/lane/variant internals, implementation narrative | one line per change, ≤4 lines each |
| `ci/<rtos>/README.md` | lane maintainers | measured build facts, toolchains, cache, linker traps, unreachable variants | adapter design | none |

Rule of thumb: if a fact already exists in another file, add a link, never a
rewrite.  Process history ("this used to…", "formerly unit-tested…")
belongs in commit messages and DtoD run artifacts, not in repository docs.

## Conventions

- Python 3.10+, PEP8, type hints, Google-style docstrings.
- Files <= 1500 lines; split when approaching the limit.
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
