# GDR

[English](README.md) | 简体中文

面向 RTOS 嵌入式固件调试的 GDB 辅助框架。

GDR 运行在 GDB Python 解释器中，提供三层调试支持，思路参考
[GEF](#致谢) 与 [Asterinas GDB helper](#致谢)：

1. **聚合命令** — `rtthread threads`、`rtthread semaphores` 等只处理
   GDB 表达式不易完成的工作：遍历集合并制表输出。
2. **Pretty-printers** — 将复杂的内核对象（`rt_mutex`、`rt_semaphore`、
   `rt_thread`）显示为一行摘要，使 `p`、`bt full`、`info locals` 更易读。
3. **便捷函数** — `$gdr_thread("main")`、`$gdr_threads()`、
   `$gdr_object(type, name)` 返回 `gdb.Value`，便于继续用原生 GDB
   表达式做字段级检查。

## 状态

### 已支持的 RTOS

| RTOS | 版本 | 状态 |
|------|------|------|
| RT-Thread | 3.1.x、4.0.x、4.1.x | 已实现；Cortex-A9 在两个版本区间均已验证，RV64 自 4.0.4 起 |
| FreeRTOS | — | 尚未实现（暂缓） |

核心实现已完成：GDB bridge、layout 引擎、pretty-printers、便捷函数、
聚合命令，以及 Cortex-A9 与 RISC-V RV64 目标上的 QEMU 闭环验证。

## 快速开始

### 前置条件

GDR 运行在 GDB Python 解释器中，因此主机 GDB 必须启用 Python 支持，
且目标固件需保留调试符号。

1. **启用 Python 的 GDB** — 用 `gdb --configuration` 确认已启用 Python
   支持。
   - ARM / RISC-V：可从
     [xPack Dev Tools](https://github.com/xpack-dev-tools/) 下载预编译工具链
   - 其他平台：从源码构建，使用
     `./configure --target="<target-triple>" --enable-targets=all --with-python`
   - 另见：[Installing GDB for ARM | Interrupt](https://interrupt.memfault.com/blog/installing-gdb#build-from-source)

2. **调试符号** — 确保被调试的 RTOS 镜像包含 DWARF / ELF 符号
   （不要 strip 你用 GDB 附加的 `.elf`）。

### 加载与初始化

```gdb
(gdb) source gdr.py
(gdb) gdr init rtthread 4.0.5
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

## 命令

| 命令 | 说明 |
|------|------|
| `rtthread threads` | 列出所有线程（name/state/priority/sp/stack_size/stack_used/max_stack_used/entry） |
| `rtthread semaphores` | 列出信号量（name/value/addr） |
| `rtthread timers` | 列出定时器（name/state/mode/type/ticks/callback） |
| `rtthread objects [type]` | 列出内核对象计数，可按类型过滤 |
| `rtthread system` | 系统摘要（tick、当前线程、对象计数、堆） |

单个对象的检查交给便捷函数 + GDB 表达式，不提供专用命令。

## 便捷函数

| 函数 | 返回值 | 示例 |
|------|--------|------|
| `$gdr_thread(name)` | `struct rt_thread` gdb.Value | `p *$gdr_thread("worker1")` |
| `$gdr_threads()` | `struct rt_thread *` 数组 | `p $gdr_threads()` / `p *$gdr_threads()[0]` |
| `$gdr_object(type, name)` | 内核对象 gdb.Value | `p *$gdr_object("SEMAPHORE", "my_sem")` |

脚本与自动化中请优先使用带引号的类型名：
`$gdr_object("SEMAPHORE", "my_sem")`。裸写 `SEMAPHORE` 仅在目标 ELF
尚未定义同名宏时才会注册为 GDB macro；若冲突，GDR 会跳过该 macro 并告警，
带引号写法始终可用。

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
该文件及其 QEMU smoke 测试必须一并审查。设计理由见
`docs/architecture.md`。

## 开发

```bash
uv sync --group dev          # 创建 .venv 并安装开发依赖
uv run pre-commit install    # 启用 git hooks
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/ -v      # 需要 QEMU + RT-Thread 固件
```

CI 运行在 [CNB](https://cnb.cool/)（Cloud Native Build）平台上，流水线定义于
`.cnb.yml`（含 lint，以及 Cortex-A9 与 RV64 QEMU 矩阵）。若要在本地
Podman machine 中复现相同的 ARM 与 RV64 QEMU 矩阵：

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
