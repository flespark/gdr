# GDR

English | [简体中文](README.zh-CN.md)

GDB helper framework for debugging RTOS-based embedded firmware.

GDR runs inside the GDB Python interpreter and provides three layers of
debugging support, following the approach popularised by the [GEF](#acknowledgements) and
the [Asterinas GDB helper](#acknowledgements):

1. **Aggregate commands** — `rtthread threads`, `rtthread semaphores`, etc.
   only handle what GDB expressions cannot easily do: iterate collections and
   tabulate results.
2. **Pretty-printers** — fold noisy kernel object (`rt_mutex`, `rt_semaphore`,
   `rt_thread`) into one-line summaries so `p`, `bt full` and `info locals`
   stay readable.
3. **Convenience functions** — `$gdr_thread("main")`, `$gdr_threads()`,
   `$gdr_object(type, name)` return `gdb.Value` so you can keep using native
   GDB expressions for the actual field inspection.

## Status

### Supported RTOS

| RTOS | Versions | Status |
|------|----------|--------|
| RT-Thread | 3.1.x,4.0.x,4.1.x | implemented; Cortex-A9 verified across both ranges, RV64 from 4.0.4 |
| FreeRTOS | — | not yet (deferred) |

Core implementation complete: GDB bridge, layout engine,
pretty-printers, convenience functions, aggregate commands, and QEMU
closed-loop verification on Cortex-A9 and RISC-V RV64 targets.

## Quick start

### Prerequisites

GDR runs inside the GDB Python interpreter, so the host GDB must be built
with Python support, and the target firmware must retain debug symbols.

1. **Python-enabled GDB** — verify with `gdb --configuration` that Python
   support is enabled.
   - ARM / RISC-V: download a prebuilt toolchain from
     [xPack Dev Tools](https://github.com/xpack-dev-tools/)
   - Other platforms: build from source with
     `./configure --target="<target-triple>" --enable-targets=all --with-python`
   - See also: [Installing GDB for ARM | Interrupt](https://interrupt.memfault.com/blog/installing-gdb#summary-of-strategies)

2. **Debug symbols** — ensure the RTOS image under debug includes DWARF /
   ELF symbols (do not strip the `.elf` you attach GDB to).

### Download the latest release

Download the RT-Thread archive from [GitHub Releases](https://github.com/flespark/gdr/releases/latest).
Use `.tar.gz` on macOS or Linux, and `.zip` on Windows.

```bash
curl -LO https://github.com/flespark/gdr/releases/latest/download/gdr-rtthread-latest.tar.gz
tar -xzf gdr-rtthread-latest.tar.gz
cd gdr-rtthread
```

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
[gdr] RT-Thread support ready. Type 'rtthread help' for commands.

(gdb) rtthread threads
(gdb) rtthread semaphores
(gdb) rtthread system
(gdb) p $gdr_thread("worker1")
```

## Commands

| Command | Description |
|---------|-------------|
| `rtthread threads` | List all threads (name/state/priority/sp/stack_size/stack_used/max_stack_used/entry) |
| `rtthread semaphores` | List semaphores (name/value/addr) |
| `rtthread timers` | List timers (name/state/mode/type/ticks/callback) |
| `rtthread objects [type]` | List kernel object counts, optionally filtered by type |
| `rtthread system` | System summary (tick, current thread, object counts, heap) |

Single-object inspection is delegated to convenience functions + GDB
expressions, not dedicated commands.

## Convenience functions

| Function | Returns | Example |
|----------|---------|---------|
| `$gdr_thread(name)` | `struct rt_thread` gdb.Value | `p $gdr_thread("worker1")` / `p $gdr_thread("worker1").stat` |
| `$gdr_threads()` | array of `struct rt_thread *` | `p $gdr_threads()` / `p *$gdr_threads()[0]` |
| `$gdr_object(type, name)` | kernel object gdb.Value | `p $gdr_object("SEMAPHORE", "my_sem")` |

Prefer the quoted type name in scripts and automation:
`$gdr_object("SEMAPHORE", "my_sem")`. Bare names such as `SEMAPHORE` are
registered as GDB macros only when the target ELF does not already define
them; on conflict GDR skips the macro and warns, and the quoted form still
works.

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
uv run pytest tests/ -v      # requires QEMU + RT-Thread firmwar
```

CI runs on [CNB](https://cnb.cool/) (Cloud Native Build); pipelines are
defined in `.cnb.yml` (lint plus Cortex-A9 and RV64 QEMU matrices). To
reproduce the same ARM and RV64 QEMU matrices locally in a Podman machine:

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
