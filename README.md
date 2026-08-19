# GDR

English | [简体中文](README.zh-CN.md)

GDB helper framework for debugging RTOS-based embedded firmware.

GDR runs inside the GDB Python interpreter and provides three layers of
debugging support, following the approach popularised by the [GEF](#acknowledgements) and
the [Asterinas GDB helper](#acknowledgements):

1. **RTOS commands** — each RTOS adapter owns its command tree (for example,
   `rtt threads` and `rtt timers`) which show you intuitive system running status.
2. **Pretty-printers** — fold noisy kernel object into one-line summaries
   so `p ui_task`, `bt full` and `info locals` stay readable.
3. **Convenience functions** — `$gdr_task("main")`, `$gdr_tasks()`,
   `$gdr_object(kind, name)` return `gdb.Value` so you can keep using native
   GDB expressions for the actual field inspection.

## Status

### Supported RTOS

| RTOS | Versions | Status |
|------|----------|--------|
| RT-Thread | 3.1.x,4.0.x,4.1.x | implemented; Cortex-A9 verified across both ranges, RV64 from 4.0.4 |
| FreeRTOS | V10.3.1 fixture baseline | Task navigation and adapter-owned `freertos tasks/system` verified on QEMU B-L475E-IOT01A |

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
[gdr] GDR loaded. Run `gdr init <rtos> <version>` to initialise support.
(gdb) gdr init rtthread 4.0.5
warning: target RT-Thread version not exported; cannot verify version
[gdr] setting up RT-Thread v4.0.5...
[gdr]   config: smp=True heap=small_mem sem=True mutex=True mb=True mq=True
[gdr]   layout: 10 structs, 2 list hooks
[gdr] rtthread commands registered (alias: rtt)
[gdr] RT-Thread support ready. Type 'rtt help' for commands.
(gdb) rtt threads
...
(gdb) rtt system
...
(gdb) rtt event test_event
...
(gdb) p test_mutex
...
```

## Commands

| Command | Description |
|---------|-------------|
| `gdr init <rtos> <version>` | Initialize the selected RTOS adapter |
| `rtt <objects>` | List the kernel object type collective status |
| `rtt <object> <name>` | Show one object's vertical detail (e.g. `rtt semaphore my_sem`) |
| `frt tasks` | List FreeRTOS tasks |
| `frt system` | Show the FreeRTOS system summary |

## Convenience functions

| Function | Returns | Example |
|----------|---------|---------|
| `$gdr_task(name)` | target-native task `gdb.Value` | `p $gdr_task("worker1")` / `p $gdr_task("worker1").stat` |
| `$gdr_tasks()` | target-native task pointer array | `p $gdr_tasks()` / `p *$gdr_tasks()[0]` |
| `$gdr_object(kind, name)` | target-native object `gdb.Value` | `p $gdr_object("semaphore", "my_sem")` |

## Pretty-printers

Kernel object types are folded into one-line summaries based on layout `summary` fields:

```gdb
(gdb) p spi1_mtx
$1 = Mutex(name="spi1_mtx", policy=PRIO, value=0, original_priority=20, hold=1, owner="main")

(gdb) p rx_sem
$2 = Semaphore(name="rx_sem", policy=FIFO, value=3)

(gdb) p pm_task
$3 = Thread(name="pm_task", sp=<pm_task_entry+0x12c>, entry=<pm_task_entry>, stack_size=2048, stat=READY, current_priority=5, init_priority=5)
```

## Note

GDR insist on data struct **keep align** between code and runtime memory. But there
maybe bias due to compiler optimization. GDR also extract info from macros in
code. Fellow debug optimized compilation options is recommend:

```cmake
add_compile_options(-O0 -ggdb3)
```

Users must specify the RTOS and major version explicitly when using GDR;
there is **no auto-detection** of the RTOS type or version (Consider variance of
version declare in RTOS and ELF compilation). Kernel *config features* (SMP, heap
manager kind, enabled IPC components) are probed automatically by symbol
presence at startup.

## Contribute

See [AGENTS.md](AGENTS.md) for the full contributor guide.

## Acknowledgements

- [GEF](https://github.com/hugsy/gef)
- [Asterinas GDB helper](https://mp.weixin.qq.com/s/mntHv8Ax0SXcTksX1xiKxA)
- [pytest-embedded](https://github.com/espressif/pytest-embedded)
