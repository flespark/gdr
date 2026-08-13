# GDR

[English](README.md) | 简体中文

面向 RTOS 嵌入式固件调试的 GDB 辅助框架。

GDR 运行在 GDB Python 解释器中，提供三层调试支持，思路参考
[GEF](#致谢) 与 [Asterinas GDB helper](#致谢)：

1. **RTOS 命令** — 每个 adapter 自己提供命令树（例如
   `rtt threads`、`rtt timers`）和对应的表格输出。
2. **Pretty-printers** — 将复杂的内核对象（`rt_mutex`、`rt_semaphore`、
   `rt_thread`）显示为一行摘要，使 `p`、`bt full`、`info locals` 更易读。
3. **便捷函数** — `$gdr_task("main")`、`$gdr_tasks()`、
   `$gdr_object(kind, name)` 返回 `gdb.Value`，便于继续用原生 GDB
   表达式做字段级检查。

## 状态

### 已支持的 RTOS

| RTOS | 版本 | 状态 |
|------|------|------|
| RT-Thread | 3.1.x、4.0.x、4.1.x | 已实现；Cortex-A9 在两个版本区间均已验证，RV64 自 4.0.4 起 |
| FreeRTOS | V10.3.1 fixture 基线 | 任务导航及 adapter 自有的 `freertos tasks/system` 已在 QEMU B-L475E-IOT01A 验证 |

核心实现已完成：GDB bridge、layout 引擎、pretty-printers、便捷函数、
RTOS 命令树，以及 Cortex-A9 与 RISC-V RV64 目标上的 QEMU 闭环验证。

FreeRTOS 支持显式版本/配置探测、基于 DWARF 字段路径的任务布局、
调度器链表导航，以及 adapter 自有的 `freertos tasks`、`freertos system`
输出。任务列按当前目标实际字段决定；队列和定时器对象枚举仍属于后续功能。
`gdr` 命令有意只保留 `gdr init` 和 `gdr help`；原始值便捷函数仍保持
RTOS 无关。

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

## 命令

| 命令 | 说明 |
|------|------|
| `gdr init <rtos> <version>` | 初始化指定的 RTOS adapter |
| `gdr help` | 显示 GDR 启动用法 |
| `rtt help` | 列出 RT-Thread 子命令和别名 |
| `rtt <object> <name>` | 以纵向 `Key: Value` 显示单个对象的详情（如 `rtt semaphore my_sem`） |
| `freertos tasks` | 列出 FreeRTOS 任务 |
| `freertos system` | 输出 FreeRTOS 系统摘要 |

列表命令（如 `rtt semaphores`、`rtt threads`、`rtt timers`）会按当前终端
宽度渲染 ASCII 表格。列集合是稳定的，绝不随终端宽度增删；只有显式标记
的弹性文本列（`Name`/`Owner`/`Waiters`/`Callback`/`Entry`）在表格过宽时
收缩，超长单元格用 `..` 截断（等待者数量在开头，截断后仍保留）。若弹性
列缩到最小宽度后仍超宽，则输出原始自然表格，允许终端自行换行。

IPC 与内存池列表以 `count:names` 摘要显示等待线程：信号量、互斥量、事件
和内存池有 `Waiters` 列，邮箱与消息队列拆分为 `RecvWait`/`SendWait`。
等待数量一律通过遍历挂起链表得出（不读旧版本才有的缓存计数），没有发送
等待链表的版本显示 `N/A` 而不是伪造的 `0`。

默认表还会从内核自身计数派生容量：邮箱与消息队列显示
`Free = capacity - entry`，内存池显示 `Used = total - free`，IPC 对象带有由
对象 flag 解码的 `FIFO`/`PRIO` 策略列。互斥量行包含 `OrigPrio` 用于优先级
继承分析，定时器显示 `Addr` 与回绕安全的 `ExpiresIn`（非活动定时器显示
`N/A`）。RT-Thread 任务列表增加 `BasePrio` 与 `Addr`；SMP 目标还会显示
`CPU`/`Bind`。FreeRTOS 任务列表使用自己的能力列，并在 TCB 提供时显示运行时间计数。
共享 renderer 只负责输出 adapter 提供的表格。

单个对象的详情也可以通过命令查看：`rtt <object> <name>`
（如 `rtt semaphore my_sem`、`rtt thread worker1`），以纵向 `Key: Value`
呈现，不干扰 `$gdr_object()` 仍返回原始 `gdb.Value` 的行为。事件详情还会
把每个等待线程与它的 `event_set` 掩码和 AND/OR/CLEAR 模式关联，帮助解释
当前事件集为何没有唤醒它。detail 还包含超越列表列的底层诊断：线程详情
显示 `Error`/`RemainingTick`，定时器详情显示回调 `Parameter`，消息队列遍历
消息链与空闲链以校验 `entry`/`max_msgs`，邮箱列出 FIFO 消息槽并校验环形
偏移，内存池报告池范围、块对齐与空闲链表一致性。所有遍历都有界且带损坏
保护。

## 便捷函数

| 函数 | 返回值 | 示例 |
|------|--------|------|
| `$gdr_task(name)` | 目标原生任务 `gdb.Value` | `p $gdr_task("worker1")` / `p $gdr_task("worker1").stat` |
| `$gdr_tasks()` | 目标原生任务指针数组 | `p $gdr_tasks()` / `p *$gdr_tasks()[0]` |
| `$gdr_object(kind, name)` | 目标原生对象 `gdb.Value` | `p $gdr_object("semaphore", "my_sem")` |

脚本与自动化请使用小写语义对象种类，例如
`$gdr_object("semaphore", "my_sem")`。返回 null 表示对象不存在，或当前
adapter 不能可靠枚举该种类。

## Pretty-printers

在 `source gdr.py` 时自动注册。内核包装类型会根据 layout 的
`summary` 字段折叠为一行摘要：

```gdb
(gdb) p mutex
$1 = Mutex(name="lock1", value=0, hold=1, owner="main")

(gdb) p semaphore
$2 = Semaphore(name="sem1", value=3)

(gdb) p thread
$3 = Thread(name="worker", stat=READY, current_priority=5)
```

## 配置

用户需显式指定 RTOS 与主版本；**不会**自动检测 RTOS 类型或版本
（在 attach / remote 场景下检测逻辑很脆弱）。内核 *配置特性*
（SMP、堆管理器类型、已启用的 IPC 组件）会在启动时通过符号是否存在
自动探测。

RT-Thread 3.1.x 仅在 Cortex-A9 QEMU BSP 上验证。上游 QEMU RV64 BSP
从 RT-Thread 4.0.4 起提供，因此 RV64 验证覆盖 4.0.4 到 4.1.1。

## 维护说明（COUPLED）

`rtthread/layout.py` 是唯一知晓 RT-Thread 结构体布局的地方。当
RT-Thread 内核结构体发生变化（新增字段、成员重命名、偏移移动）时，
该文件及其 QEMU smoke 测试必须一并审查。RT-Thread 中间展示模型及其
`gdb.Value` 转换器归 `rtthread/adapter.py` 所有，它们不是 ABI 布局描述。
设计理由见 `docs/architecture.md`。

## 开发

```bash
uv sync --group dev          # 创建 .venv 并安装开发依赖
uv run pre-commit install    # 启用 git hooks
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit --cov
uv run pytest tests/integration -v  # QEMU fixture 可用时运行
```

FreeRTOS smoke test 会构建锁定的 STM32CubeL4 `v1.18.2`
B-L475E-IOT01A fixture（其中 FreeRTOS submodule 固定为 commit
`5fe3a380e5eadb6ce0a5149725210c3fe70d1c15`），并在 QEMU 中运行：

```bash
bash ci/freertos/run-qemu-matrix.sh
```

该命令需要 `qemu-system-arm`、启用 Python 的 `gdb-multiarch` 与
`arm-none-eabi-gcc`。fixture 使用 Cortex-M SysTick port 与 QEMU semihosting，
不依赖该板卡 QEMU 尚未实现的 LPTIM。

CI 运行在 [CNB](https://cnb.cool/)（Cloud Native Build）平台上，流水线定义于
`.cnb.yml`（含 lint、GDB 12 最低兼容基线，以及 Cortex-A9 与 RV64 QEMU
矩阵）。若要在本地 Podman machine 中复现当前的 ARM 与 RV64 QEMU 矩阵：

```bash
ci/validate-podman.sh
```

该脚本会为 `linux/amd64` 构建 `ci/Dockerfile`，并使用固定版本的
xPack 工具链。运行前请先启动 Podman machine。

完整贡献指南见 `AGENTS.md`。

## 致谢

- [GEF](https://github.com/hugsy/gef)
- [Asterinas GDB helper](https://mp.weixin.qq.com/s/mntHv8Ax0SXcTksX1xiKxA)
- [pytest-embedded](https://github.com/espressif/pytest-embedded)
