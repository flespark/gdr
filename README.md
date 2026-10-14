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
| FreeRTOS | V10.3.0–V11.3.x | implemented; verified on QEMU MPS2-AN385 (Cortex-M3, 10.3.1/10.4/10.5/11.1) and MPS2-AN521 (dual Cortex-M33, SMP, 11.3.1) |

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

Download the unified GDR archive, which includes both RT-Thread and FreeRTOS
adapters, from [GitHub Releases](https://github.com/flespark/gdr/releases/latest).

Use `.tar.gz` on macOS or Linux:

```bash
curl -LO https://github.com/flespark/gdr/releases/latest/download/gdr-latest.tar.gz
tar -xzf gdr-latest.tar.gz
cd gdr
```

and `.zip` on Windows:

```powershell
Invoke-WebRequest https://github.com/flespark/gdr/releases/latest/download/gdr-latest.zip -OutFile gdr-latest.zip
Expand-Archive -Path .\gdr-latest.zip -DestinationPath .
Set-Location .\gdr
```

### Load and init

```gdb
(gdb) source ~/Project/gdr/gdr.py
[gdr] GDR loaded. Run `gdr init <rtos> <version>` to initialise support.
(gdb) source gdr.py
(gdb) gdr init rtt 4.0.5
[gdr] setting up RT-Thread v4.0.5...
[gdr]   config: smp: enable, cores: 2, heap: small_mem, semaphore: enable, mutex: enable, event: enable, mailbox: enable, messagequeue: enable, mempool: enable, timer-soft: enable, memtrace: enable
[gdr]   layout: 11 structs, 2 list hooks
[gdr] rtthread commands registered (alias: rtt)
[gdr] RT-Thread support ready. Type 'rtt help' for commands.
(gdb) rtt system
   Current task: main
    Task number: 24
     Tick count: 103
Scheduler state: Unlocked
          Ready: 17
        Suspend: 6
        Running: 1
         device: 4
          event: 2
        mailbox: 8
        memheap: 0
        mempool: 3
       msgqueue: 3
          mutex: 6
      semaphore: 8
           task: 24
          timer: 25
 Heap allocator: small_mem
      Heap used: 16108 B
     Heap total: 7630516 B
    Heap status: good
(gdb) rtt tasks
Name          State    Prio  BasePrio  PC                      SP          Stack  Used  HighWater  Entry                      CPU  Bind  Addr
------------  -------  ----  --------  ----------------------  ----------  -----  ----  ---------  -------------------------  ---  ----  ----------
locker        Ready    20    20        <locker_entry>          0x600a1498  1024   76    76         <locker_entry>             N/A  N/A   0x600a40e4
tshell        Ready    20    20        <finsh_thread_entry>    0x600bcbe8  4096   76    76         <finsh_thread_entry>       N/A  N/A   0x600bbb78
aio           Suspend  16    16        <rt_schedule+584>       0x600bb4a0  2048   140   140        <_workqueue_thread_entry>  N/A  N/A   0x600bac70
sys_work      Suspend  23    23        <rt_schedule+584>       0x600baa80  2048   140   140        <_workqueue_thread_entry>  N/A  N/A   0x600ba250
mmcsd_detect  Suspend  22    22        <rt_schedule+584>       0x600b7a90  1024   184   476        <mmcsd_detect>             N/A  N/A   0x600b769c
tcpip         Suspend  10    10        <rt_schedule+584>       0x600b9f40  1024   660   716        <tcpip_thread>             N/A  N/A   0x600b9d18
etx           Ready    12    12        <rt_schedule+584>       0x600b6ad8  1024   180   264        <eth_tx_thread_entry>      N/A  N/A   0x600b66c0
erx           Ready    12    12        <rt_schedule+584>       0x600b6f80  1024   272   648        <eth_rx_thread_entry>      N/A  N/A   0x600b6bc4
tsystem       Suspend  30    30        <rt_schedule+584>       0x600a5d48  1024   164   180        <rt_thread_system_entry>   N/A  N/A   0x600a5940
tidle1        Ready    31    31        <rt_thread_idle_entry>  0x600a58f8  1024   72    72         <rt_thread_idle_entry>     N/A  1     0x600a5094
tidle0        Ready    31    31        <idle_wfi+12>           0x600a54dc  1024   100   212        <rt_thread_idle_entry>     N/A  0     0x600a4fe8
timer         Suspend  4     4         <rt_schedule+584>       0x600a4f28  1024   180   180        <_timer_thread_entry>      N/A  N/A   0x600a4b30
main *        Running  10    10        <main+2556>             0x600b9b80  2048   212   752        <main_thread_entry>        0    N/A   0x600b9398
(gdb) rtt heap
Algorithm: small_mem
TotalSize: 7630516 B
 UsedSize: 16108 B
  MaxUsed: 16108 B
 MemTrace: enabled
   Blocks: 36 used, 2 free, 38 total
(gdb) rtt heap dump
[gdr] small_mem: 38 blocks, 16108 B used, 7614424 B free, arena 7630532 B
[gdr]   owner:main  12228 B  #e15e3a
[gdr]   owner:NONE  2856 B  #3aa66c
[gdr]   owner:erx  628 B  #9b59b6
[gdr]   owner:tcpi  396 B  #e8b11c
[gdr] heap map written to /Users/simon/Source/rt-thread/gdr-heap-rtthread-gen0.png (5131 bytes)
(gdb) rtt mtxs
Name         Value  Hold  OrigPrio  Owner  Policy  Waiters  Addr
-----------  -----  ----  --------  -----  ------  -------  ----------
wait_mutex   1      0     255       N/A    PRIO    0        0x600a0994
test_mutex   1      0     255       N/A    PRIO    0        0x600a0968
sal_lock     1      0     255       N/A    PRIO    0        0x600acb1c
sd_bus_lock  1      0     255       N/A    PRIO    0        0x600bab54
fslock       1      0     255       N/A    PRIO    0        0x600b7238
mu0          0      1     10        tcpip  PRIO    0        0x600b9cdc
```

## Commands

| Command | Description |
| --------- | ------------- |
| `gdr init <rtos> <version>` | Initialize the selected RTOS adapter |
| `gdr reset` | Drop the active adapter and session caches |
| `gdr help [topic]` | Show bootstrap or pretty-printer help |
| `<rtos> help [topic]` | Browse the command tree; a topic documents fields, tips, configuration dependencies and limitations |
| `<rtos> <objects>` | List the kernel object type collective status |
| `<rtos> <object> <name-or-address>` | Show one object's details as a vertical `Key: Value` block |
| `<rtos> check [scope] [name]` | Run complete consistency checks (`rtt help check`) |
| `<rtos> watch [<kind> \| <object> <target>]` / `<rtos> unwatch …` | Print watched kinds/objects on every stop, like `display` of gdb but never stops the target |
| `<rtos> heap dump [path]` | Draw the heap occupancy bitmap. Inline in iTerm2 or Kitty, otherwise written to a PNG |

## Convenience functions

| Function | Returns | Example |
| ---------- | --------- | --------- |
| `$gdr_task(name)` | target-native task `gdb.Value` | `p $gdr_task("worker1")` / `p $gdr_task("worker1").stat` |
| `$gdr_tasks()` | target-native task pointer array | `p $gdr_tasks()` / `p *$gdr_tasks()[0]` |
| `$gdr_object(kind, target)` | target-native object `gdb.Value` | `p $gdr_object("semaphore", "my_sem")` |

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

GDR insist on data struct **keep align** between RTOS code and runtime memory. But there
maybe bias due to compiler optimization. GDR also infer RTOS version, component/feature
enable from macros in ELF DWARF. Fellow debug optimized compilation options is recommend:

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
