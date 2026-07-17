# GDR

GDB helper framework for debugging RTOS-based embedded firmware.

GDR runs inside the GDB Python interpreter and provides three layers of
debugging support, following the approach popularised by the Linux kernel
`scripts/gdb/` and the Asterinas GDB helper:

1. **Pretty-printers** — fold noisy wrapper types (`rt_mutex`, `rt_semaphore`,
   `rt_thread`) into one-line summaries so `p`, `bt full` and `info locals`
   stay readable.
2. **Convenience functions** — `$gdr_thread("main")`, `$gdr_threads()`,
   `$gdr_object(type, name)` return `gdb.Value` so you can keep using native
   GDB expressions for the actual field inspection.
3. **Aggregate commands** — `rtthread threads`, `rtthread semaphores`, etc.
   only handle what GDB expressions cannot easily do: iterate collections and
   tabulate results.

## Status

Core implementation complete: GDB bridge, layout engine,
pretty-printers, convenience functions, aggregate commands, and QEMU
closed-loop verification on Cortex-A9 and RISC-V RV64 targets.

## Supported RTOS

| RTOS | Versions | Status |
|------|----------|--------|
| RT-Thread | 4.0.0-4.1.1 | implemented, Cortex-A9 and RV64 QEMU verified |
| FreeRTOS | — | not yet (deferred) |

## Quick start

```gdb
(gdb) source gdr.py
(gdb) gdr rtthread 4.0.5
warning: target RT-Thread version not exported; cannot verify --version
[gdr] setting up RT-Thread v4.0.5...
[gdr]   config: smp=True heap=small_mem sem=True mutex=True mb=True mq=True
[gdr]   layout: 10 structs, 2 list hooks
[gdr] rtthread commands registered (alias: rtt)
[gdr] RT-Thread support ready. Type 'rtthread help' for commands.

(gdb) rtthread threads
(gdb) rtthread semaphores
(gdb) rtthread system
(gdb) p *$gdr_thread("worker1")
```

## Commands

| Command | Description |
|---------|-------------|
| `rtthread threads` | List all threads (name/state/priority/sp/stack_size/entry) |
| `rtthread semaphores` | List semaphores (name/value/addr) |
| `rtthread timers` | List timers (name/state/mode/type/ticks/callback) |
| `rtthread objects [type]` | List kernel object counts, optionally filtered by type |
| `rtthread system` | System summary (tick, current thread, object counts, heap) |

Single-object inspection is delegated to convenience functions + GDB
expressions, not dedicated commands.

## Convenience functions

| Function | Returns | Example |
|----------|---------|---------|
| `$gdr_thread(name)` | `struct rt_thread` gdb.Value | `p *$gdr_thread("worker1")` |
| `$gdr_threads()` | first thread gdb.Value | `p *$gdr_threads()` |
| `$gdr_object(type_code, name)` | kernel object gdb.Value | `p *$gdr_object(0x02, "my_sem")` |

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

Run the same ARM and RV64 QEMU matrices as CNB in a local Podman machine:

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
