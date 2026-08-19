# GDR

[English](README.md) | 简体中文

面向 RTOS 嵌入式固件调试的 GDB 辅助框架。

GDR 运行在 GDB Python 解释器中，提供三层调试支持，思路参考
[GEF](#致谢) 与 [Asterinas GDB helper](#致谢)：

1. **RTOS 命令** — 每个 RTOS adapter 提供自己的命令树（例如
   `rtt threads`、`rtt timers`）用于获取直观的系统运行状态。
2. **Pretty-printers** — 将复杂的内核对象 显示为一行摘要，使 `p ui_task`、
   `bt full`、`info locals` 更易读。
3. **便捷函数** — `$gdr_task("main")`、`$gdr_tasks()`、
   `$gdr_object(kind, name)` 返回 `gdb.Value`，便于继续用原生 GDB
   表达式做字段级检查。

## 状态

### 已支持的 RTOS

| RTOS | 版本 | 状态 |
|------|------|------|
| RT-Thread | 3.1.x、4.0.x、4.1.x | 已实现；Cortex-A9 在两个版本区间均已验证，RV64 自 4.0.4 起 |
| FreeRTOS | V10.3.1 fixture 基线 | 任务导航及 adapter 自有的 `freertos tasks/system` 已在 QEMU B-L475E-IOT01A 验证 |

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
下载包含 RT-Thread 支持的 GDR 脚本压缩包。

macOS / Linux 使用 `.tar.gz`:

```bash
tar -xzf gdr-rtthread.tar.gz
cd gdr-rtthread
```

Windows 使用原生支持的 `.zip`:

```powershell
Expand-Archive -Path .\gdr-rtthread.zip -DestinationPath .
Set-Location .\gdr-rtthread
```

### 加载与初始化

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

## 命令

| 命令 | 说明 |
|------|------|
| `gdr init <rtos> <version>` | 初始化指定的 RTOS adapter |
| `rtt <objects>` | 列表形式展示指定内核对象的总体状态 |
| `rtt <object> <name>` | 以纵向 `Key: Value` 显示单个对象的详情（如 `rtt semaphore my_sem`） |
| `frt tasks` | 列出 FreeRTOS 任务 |
| `frt system` | 输出 FreeRTOS 系统摘要 |

## 便捷函数

| 函数 | 返回值 | 示例 |
|------|--------|------|
| `$gdr_task(name)` | 目标原生任务 `gdb.Value` | `p $gdr_task("worker1")` / `p $gdr_task("worker1").stat` |
| `$gdr_tasks()` | 目标原生任务指针数组 | `p $gdr_tasks()` / `p *$gdr_tasks()[0]` |
| `$gdr_object(kind, name)` | 目标原生对象 `gdb.Value` | `p $gdr_object("semaphore", "my_sem")` |

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

GDR 认为运行时内存中的数据结构和代码是**一致的**，没有考虑经过编译优化产生偏差的情况。
且部分信息依据代码中的宏定义，所以最好使用如下调试优化的编译选项：

```cmake
add_compile_options(-O0 -ggdb3)
```

使用 GDR 时，用户需显式指定 RTOS 与版本；GDR **不会**自动检测 RTOS 类型或版本
（考虑 RTOS 实现和编译配置的差异， 可能无法从 ELF 推断出准确的 RTOS 版本）。
内核 *配置特性* （SMP、堆管理器类型、已启用的 IPC 组件）会在启动时通过符号是否
存在自动探测。

## 贡献

完整贡献指南见 [AGENTS.md](AGENTS.md) 。

## 致谢

- [GEF](https://github.com/hugsy/gef)
- [Asterinas GDB helper](https://mp.weixin.qq.com/s/mntHv8Ax0SXcTksX1xiKxA)
- [pytest-embedded](https://github.com/espressif/pytest-embedded)
