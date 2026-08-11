# GDR

English | [简体中文](README.zh-CN.md)

GDB helper framework for debugging RTOS-based embedded firmware.

GDR runs inside the GDB Python interpreter and provides three layers of
debugging support, following the approach popularised by the [GEF](#acknowledgements) and
the [Asterinas GDB helper](#acknowledgements):

1. **RTOS commands** — each adapter owns its command tree (for example,
   `rtt threads` and `rtt timers`) and the corresponding table output.
2. **Pretty-printers** — fold noisy kernel object (`rt_mutex`, `rt_semaphore`,
   `rt_thread`) into one-line summaries so `p`, `bt full` and `info locals`
   stay readable.
3. **Convenience functions** — `$gdr_task("main")`, `$gdr_tasks()`,
   `$gdr_object(kind, name)` return `gdb.Value` so you can keep using native
   GDB expressions for the actual field inspection.

## Status

### Supported RTOS

| RTOS | Versions | Status |
|------|----------|--------|
| RT-Thread | 3.1.x,4.0.x,4.1.x | implemented; Cortex-A9 verified across both ranges, RV64 from 4.0.4 |
| FreeRTOS | V10.3.1 fixture baseline | Phase 2 task navigation and `freertos tasks/system` verified on QEMU B-L475E-IOT01A |

Core implementation complete: GDB bridge, layout engine,
pretty-printers, convenience functions, RTOS command trees, and QEMU
closed-loop verification on Cortex-A9 and RISC-V RV64 targets.

FreeRTOS Phase 2 adds explicit version/config probing, DWARF-path task layouts,
scheduler-list navigation, and the `freertos tasks` / `freertos system` commands.
Queue and timer object enumeration remains a Phase 3 feature. The `gdr`
command is intentionally limited to `gdr init` and `gdr help`; raw-value
convenience functions remain RTOS-neutral.

## Quick start

### Prerequisites

1. **Python-enabled GDB** — verify the interpreter GDB will actually use:

   ```bash
   gdb --nx --quiet --batch -ex 'python import sys; print(sys.version)'
   ```

   The reported version should be 3.10 or newer for a supported configuration.
   GDB with older python version may work but not tested.

   - ARM / RISC-V: download a prebuilt toolchain from
     [xPack Dev Tools](https://github.com/xpack-dev-tools/)
   - Other platforms: build from source with
     `./configure --target="<target-triple>" --enable-targets=all --with-python`
   - See also: [Installing GDB for ARM | Interrupt](https://interrupt.memfault.com/blog/installing-gdb#summary-of-strategies)

2. **Debug symbols** — ensure the RTOS image under debug includes DWARF /
   ELF symbols (do not strip the `.elf` you attach GDB to).

### Download

Download the RT-Thread archive from [GitHub Releases](https://github.com/flespark/gdr/releases/latest).

Use `.tar.gz` on macOS or Linux:

```bash
curl -LO https://github.com/flespark/gdr/releases/latest/download/gdr-rtthread-latest.tar.gz
tar -xzf gdr-rtthread-latest.tar.gz
cd gdr-rtthread
```

and `.zip` on Windows:

```powershell
Invoke-WebRequest https://github.com/flespark/gdr/releases/latest/download/gdr-rtthread-latest.zip -OutFile gdr-rtthread-latest.zip
Expand-Archive -Path .\gdr-rtthread-latest.zip -DestinationPath .
Set-Location .\gdr-rtthread
```

### Load and init

```gdb
(gdb) source gdr.py
(gdb) gdr init rtthread 4.0.5
[gdr] setting up RT-Thread v4.0.5...
[gdr]   config: smp=True heap=small_mem sem=True mutex=True mb=True mq=True
[gdr]   layout: 10 structs, 2 list hooks
[gdr] rtthread commands registered (alias: rtt)
[gdr] RT-Thread support ready. Type 'rtt help' for commands.

(gdb) rtt threads
(gdb) rtt timers
(gdb) rtt system
(gdb) p $gdr_task("worker1")
```

## Commands

| Command | Description |
|---------|-------------|
| `gdr init <rtos> <version>` | Initialize the selected RTOS adapter |
| `gdr help` | Show GDR bootstrap usage |
| `rtthread ...` / `rtt ...` | RT-Thread command tree: `threads`, `semaphores`, `mutexes`, `events`, `mailboxs`, `messagequeues`, `mempools`, `timers`, and `system`; short aliases include `tasks`, `sems`, `mtxs`, `msgs`, and `mboxs` |
| `freertos tasks` | List FreeRTOS tasks |
| `freertos system` | Show the FreeRTOS system summary |

Single-object inspection is delegated to convenience functions + GDB
expressions, not dedicated commands.

## Convenience functions

| Function | Returns | Example |
|----------|---------|---------|
| `$gdr_task(name)` | target-native task `gdb.Value` | `p $gdr_task("worker1")` / `p $gdr_task("worker1").stat` |
| `$gdr_tasks()` | target-native task pointer array | `p $gdr_tasks()` / `p *$gdr_tasks()[0]` |
| `$gdr_object(kind, name)` | target-native object `gdb.Value` | `p $gdr_object("semaphore", "my_sem")` |

Use lower-case semantic object kinds in scripts and automation, such as
`$gdr_object("semaphore", "my_sem")`. A null result means that the object was
not found or that the selected adapter cannot reliably enumerate that kind.

## Pretty-printers

Registered automatically on `source gdr.py`. Kernel wrapper types are
folded into one-line summaries based on layout `summary` fields:

```gdb
(gdb) p mutex
$1 = Mutex(name="lock1", value=0, hold=1, owner="main")

(gdb) p semaphore
$2 = Semaphore(name="sem1", value=3)

(gdb) p thread
$3 = Thread(name="worker", stat=READY, current_priority=5)
```

## Configuration

Users specify the RTOS and major version explicitly; there is **no
auto-detection** of the RTOS type or version (detection logic is fragile
across attach/remote scenarios). Kernel *config features* (SMP, heap
manager kind, enabled IPC components) are probed automatically by symbol
presence at startup.

RT-Thread 3.1.x is verified on the Cortex-A9 QEMU BSP only. The upstream
QEMU RV64 BSP starts at RT-Thread 4.0.4, so RV64 verification covers 4.0.4
through 4.1.1.

## Maintenance notes (COUPLED)

`rtthread/layout.py` is the single place that knows RT-Thread struct
layouts. When an RT-Thread kernel struct changes (new field, renamed
member, shifted offset), that file — and its QEMU smoke test — must be
reviewed together. See `docs/architecture.md` for the rationale.

## Development

```bash
uv sync --group dev          # create .venv and install dev dependencies
uv run pre-commit install    # activate git hooks
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit --cov
uv run pytest tests/integration -v  # runs when QEMU fixtures are available
```

The FreeRTOS Phase 1 smoke test builds the locked STM32CubeL4 `v1.18.2`
B-L475E-IOT01A fixture (whose FreeRTOS submodule is commit
`5fe3a380e5eadb6ce0a5149725210c3fe70d1c15`) and runs it under QEMU:

```bash
bash ci/freertos/run-qemu-matrix.sh
```

It needs `qemu-system-arm`, a Python-enabled `gdb-multiarch`, and an
`arm-none-eabi-gcc` toolchain. The fixture uses the Cortex-M SysTick port and
QEMU semihosting, not the board's unsupported LPTIM.

CI runs on [CNB](https://cnb.cool/) (Cloud Native Build); pipelines are
defined in `.cnb.yml` (lint, the GDB 12 compatibility baseline, and Cortex-A9
and RV64 QEMU matrices). To reproduce the current ARM and RV64 QEMU matrices
locally in a Podman machine:

```bash
ci/validate-podman.sh
```

The script builds `ci/Dockerfile` for `linux/amd64` and uses the pinned xPack
toolchains. Start a Podman machine before running it.

See `AGENTS.md` for the full contributor guide.

## Acknowledgements

- [GEF](https://github.com/hugsy/gef)
- [Asterinas GDB helper](https://mp.weixin.qq.com/s/mntHv8Ax0SXcTksX1xiKxA)
- [pytest-embedded](https://github.com/espressif/pytest-embedded)
