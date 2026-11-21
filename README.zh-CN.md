# GDR

[English](README.md) | 简体中文

面向 RTOS 嵌入式固件调试的 GDB 辅助框架。

GDR 运行在 GDB Python 解释器中，提供三层调试支持，思路参考
[GEF](#致谢) 与 [Asterinas GDB helper](#致谢)：

1. **RTOS 命令** — 每个 RTOS adapter 提供自己的命令树（例如
   `rtt threads`、`rtt timers`）用于获取直观的系统运行状态。
2. **Pretty-printers** — 将已知的内核结构体折叠为一行摘要，仅在 GDB 打印该值时生效
  （例如 `p ui_task`）。
3. **便捷函数** — `$gdr_task("main")`、`$gdr_tasks()`、
   `$gdr_object(kind, name)` 返回 `gdb.Value`，便于继续用原生 GDB
   表达式做字段级检查。

## 状态

### 已支持的 RTOS

| RTOS | 版本 | 状态 |
|------------|-------------------------------|----------------------------------------------------------------------------------------------------------------------------|
| RT-Thread  | 3.1.x、4.0.x、4.1.x           | 已实现；Arm M/A-profile 和 RV32/RV64；[已验证的版本、端口与限制](docs/boundary.md#rt-thread) |
| FreeRTOS   | V10.3.0–V11.3.x               | 已实现；Armv6-M/v7-M/v7E-M/v8-M Mainline（SMP/MPU）与 RV32/RV64；[已验证的版本、端口与限制](docs/boundary.md#freertos) |

## 快速开始

### 前置条件

1. **启用 Python 的 GDB** — 检查 GDB 实际将使用的解释器：

   ```bash
   gdb --nx --quiet --batch -ex 'python import sys; print(sys.version)'
   ```

   对于启用 Python 的 GDB，要求输出版本不低于 3.10。内置更旧版本 Python 或许能工作但未经完整测试。

   - ARM / RISC-V：可从
     [xPack Dev Tools](https://github.com/xpack-dev-tools/) 下载预编译工具链
   - 其他平台：从源码构建，使用
     `./configure --target="<target-triple>" --enable-targets=all --with-python`
   - 另见：[Installing GDB for ARM | Interrupt](https://interrupt.memfault.com/blog/installing-gdb#summary-of-strategies)

2. **调试符号** — 确保被调试的 RTOS 镜像包含 DWARF / ELF 符号
   （不要 strip 你用 GDB 附加的 `.elf`）。

### 下载

请从 [CNB Releases](https://cnb.cool/flespark-2026/gdr/-/releases) 的最新 Release
下载包含所有 RTOS 支持 的 GDR 脚本压缩包

macOS / Linux 使用 `.tar.gz`:

```bash
tar -xzf gdr.tar.gz
cd gdr
```

Windows 使用原生支持的 `.zip`:

```powershell
Expand-Archive -Path .\gdr.zip -DestinationPath .
Set-Location .\gdr
```

### 加载与初始化

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

## 命令

| 命令 | 说明 |
| ------ | ------ |
| `gdr init <rtos> <version>` | 初始化指定的 RTOS adapter |
| `gdr reset` | 清除当前 adapter 与会话缓存 |
| `gdr help [topic]` | 查看初始化或 pretty-printer 帮助 |
| `<rtos> help [topic]` | 浏览命令树；topic 包含字段说明、使用 tips、配置依赖与实现限制 |
| `<rtos> <objects>` | 列表形式展示指定内核对象的总体状态 |
| `<rtos> <object> <name-or-address>` | 以纵向 `Key: Value` 显示指定对象的详情 |
| `<rtos> backtrace [<task>] [--max-depth N]` | 通过 EHABI、DWARF CFI、指令证明的叶子返回或验证过的帧指针链回溯指定任务或当前 PC 的栈；输出帧 PC、符号/源码、方法、完整性和停止原因 |
| `<rtos> check [scope] [name]` | 显式运行完整一致性检查 |
| `<rtos> watch [<kind> \| <object> <target>]` / `<rtos> unwatch …` | 每次停机打印被 watch 的对象，类似gdb的 `display` 但不会使目标停下 |
| `<rtos> heap dump [path]` | 绘制堆占用位图，iTerm2 或 Kitty 中内联显示，否则写入 PNG |

## 便捷函数

| 函数 | 返回值 | 示例 |
| ------ | -------- | ------ |
| `$gdr_task(name)` | 目标原生任务 `gdb.Value` | `p $gdr_task("worker1")` / `p $gdr_task("worker1").stat` |
| `$gdr_tasks()` | 目标原生任务指针数组 | `p $gdr_tasks()` / `p *$gdr_tasks()[0]` |
| `$gdr_object(kind, target)` | 目标原生对象 `gdb.Value` | `p $gdr_object("semaphore", "my_sem")` |

## Pretty-printers

内核对象会根据 layout 的 `summary` 字段折叠为一行摘要：

```gdb
(gdb) p spi1_mtx
$1 = Mutex(name="spi1_mtx", policy=PRIO, value=0, original_priority=20, hold=1, owner="main")

(gdb) p rx_sem
$2 = Semaphore(name="rx_sem", policy=FIFO, value=3)

(gdb) p pm_task
$3 = Thread(name="pm_task", sp=<pm_task_entry+0x12c>, entry=<pm_task_entry>, stack_size=2048, stat=READY, current_priority=5, init_priority=5)
```

## 注意

使用 GDR 时需显式指定 RTOS 与版本（**不自动检测**），内核特性在初始化时通过符号
探测。GDR 依赖与源码一致的内存布局并通过未裁剪的 ELF 读取 DWARF 宏与栈回溯表；
固件构建须满足以下配置：

- **编译选项（必需）**：明确架构/ABI 参数（如 Arm `-mcpu`/`-mfloat-abi` 或
  RISC-V `-march`/`-mabi`）、`-g3 -gdwarf-4`（生成 DWARF-4 `.debug_macro` 与
  `.debug_frame`）以及 `-funwind-tables`（生成 `.ARM.exidx`/`.eh_frame`）；推荐
  同时开启 `-O0`/`-Og`、`-fno-lto` 并在汇编选项中保留 `-g3 -gdwarf-4`。
- **链接与 RTOS 配置（栈回溯必需）**：保留提供给 GDB 的 `.elf` 完整符号与表项
  （在 `--gc-sections` 下通过 `KEEP(*(.eh_frame))` / `*(.ARM.exidx*)` 保留），
  FreeRTOS 启用 `configRECORD_STACK_HIGH_ADDRESS=1`，并导出 MSP/ISR 栈边界以
  支持跨栈 IRQ/Trap 回溯。

Arm M-profile 跨栈 IRQ 回溯自动从链接符号或段解析中断栈与向量容量；精确 SCB Fault
结束于 `synchronous-fault`，非精确 Fault 返回 `fault-frame-imprecise`。完整回溯
流程、覆盖符号及构建细节见 [docs/backtrace.md](docs/backtrace.md)。

## 贡献

完整贡献指南见 [AGENTS.md](AGENTS.md) 。

## 致谢

- [GEF](https://github.com/hugsy/gef)
- [Asterinas GDB helper](https://mp.weixin.qq.com/s/mntHv8Ax0SXcTksX1xiKxA)
- [pytest-embedded](https://github.com/espressif/pytest-embedded)
