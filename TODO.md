# TODO — GDR 改进路线图

基于 GEF 工程实践对比分析得出。每项标注优先级 (P0/P1/P2)、所在文件、
与 GEF 的对应做法。已完成项保留并标记 ✅。

---

## P0 — 闭环验证流水线（当前焦点）

### 0.1 选择流水线平台：GitHub Actions vs CNB

当前仓库 git 远端指向 GitHub (`flespark/gdr`)，CI 仅有 lint
(`ci/lint.yml`)。测试 ELF 与 QEMU 工具链都在本地，未被流水线覆盖，
导致"仓库上下文不一致"。

**对比结论（详见 §决策依据）：统一采用 CNB。**

| 维度 | GitHub Actions | CNB (cnb.cool) |
|------|---------------|----------------|
| 国内拉取速度 | 慢，常需代理 | 快，腾讯云国内节点 |
| QEMU system ARM 支持 | 需 apt 装 qemu-system-arm | 同（容器内 apt） |
| 自定义镜像缓存 | 外部 registry，GHCR 限速 | 自带制品库 `docker.cnb.cool` |
| 定价 | 公开仓库免费 / 私有按分钟 | 公开仓库免费 / 私有按核时 |
| 远程调试入口 | 无 | `vscode` service 可一键登录复现 |
| LLMs 文档 | 英文为主 | 中文文档 + cnb cli 本地知识库 |

**决策：CI 全部统一到 CNB，关闭 GitHub 侧流水线。** 理由：

1. 单一配置源 `.cnb.yml`，lint + qemu-test 在一个文件里，改规则不用双向同步。
2. `docker.build` 镜像哈希自动缓存到 CNB 制品库，lint 与 qemu-test 共享同一镜像缓存。
3. vscode 远程调试入口对 lint 失败也有用，比 GitHub dry logs 强。
4. CNB 仓库可公开，README 嵌入 badge 可见性与 GitHub 平齐。
5. 国内拉取 RT-Thread 源码 + apt 工具链准备 <2 分钟，比 GitHub 快 5-10 分钟。

### 0.2 实现 CNB 流水线（已落地 ✅）

已交付的产物：

- `ci/Dockerfile` — **通用基础镜像**（不挂任何特定 RTOS 名下）：QEMU、
  gdb-multiarch、固定版本的 xPack ARM/RISC-V GCC/newlib、scons 与 uv。所有
  RTOS × 硬件平台组合共用。前期单 Dockerfile 维护负担最小；CNB 不支持并行
  启动多个虚拟机，也限制了多套工具链镜像并行运行的实际收益。
- `ci/rt-thread/` — RT-Thread 专属资源（build 脚本 + patches）。RTOS 拆分在
  这一层，Dockerfile 不拆。
- `ci/rt-thread/build-rtt.sh` — 幂等的 RT-Thread 编译脚本：clone v4.0.5 →
  应用补丁 → scons。本地端到端验证可跑通。
- `ci/rt-thread/patches/<平台>/<版本>/*.patch` — 平台和版本隔离的补丁：
  - `001-test-fixture-main.patch` — 改造 main.c 生成 worker1/2/3 +
    test_sem + test_mutex + test_timer
  - `002-disable-pthreads.patch` / `003-warn-fix.patch` — 编译修复
  - `004-newlib-sdidinit.patch` — newlib 4.x 兼容
  - `005-scons-deque-list.patch` — SCons 4.x deque 兼容
- `.cnb.yml` — 两条流水线：
  - `lint`（push + PR）：ruff check/format，用平台默认镜像快速跑
  - `qemu-test`（push + PR）：amd64 + cpus:4 + Dockerfile 缓存 +
    scons 构建 ELF + pytest QEMU 闭环
- `ci/validate-podman.sh` — 本地按 CNB 的 linux/amd64 镜像运行 ARM 与 RV64
  两个 QEMU 矩阵。
- `tests/conftest.py` — `ELF_PATH` 默认优先 `tests/fixtures/`，再回退
  `~/Source`；`GDR_ELF_PATH` 环境变量覆盖（CI 用）。
- 删除 `ci/lint.yml` 与 `.github/` —— GitHub 侧不再跑流水线。

**测试目标：vexpress-a9 (Cortex-A9)** —— 这是 RT-Thread 测内核框架的
事实标杆（外设丰富，能模拟 SD/MMC/LCD/网卡）。**虽是 Cortex-A 不是 M**，
但 GDR 关心的是内核数据结构布局（`rt_thread` 等），这些跨 A/M 共用同一
份内核代码（仅 libcpu 上下文切换 / SVC 实现不同；调度对象布局一致）。
若将来要覆盖真实 Cortex-M 硬件调试链路，应通过 OpenOCD + 物理板做长周期
manual 跑 —— QEMU 不转向 mps2 (Cortex-M 外设贫瘠反而降低覆盖度)。

### 0.3 在 CNB 远端验证（已跑通 ✅）

- [x] 把仓库镜像到 CNB（`git remote add cnb <您的cnb仓库地址>` 后 push）
- [x] 首次 push 触发 lint + qemu-test 两条流水线
- [x] lint 与 Cortex-A9 qemu-test 均成功，端到端闭环完成
- [x] 在 README 顶部嵌入 CNB badge

### 0.4 Dockerfile 拆分触发点（未来演化备注）

**当前**：单份 `ci/Dockerfile` 覆盖所有 RTOS × 硬件平台组合。维护负担最低，
CNB 缓存最大化。

**何时拆分**：仅当出现**真正不可共存的依赖**时才拆，例如：

- 某 RTOS 要 python2 而另一些要 python3
- 某硬件平台要 GCC-9 而另一些要 GCC-14
- 某工具的安装冲突另一个工具

在上述冲突出现前，不预拆分。预拆分纯属为不存在的需求买单，且 CNB 不支持
单一 job 内并行多个虚拟机/容器实例，多镜像隔离的运行时收益同样受限。

### 0.5 RISC-V RV64 QEMU 闭环验证

**目标：** `qemu-system-riscv64 -machine virt -cpu rv64`。使用 RT-Thread
上游 QEMU VIRT BSP，以 64 位指针和 `RT_ALIGN_SIZE=8` 覆盖 GDR 在 32 位
Cortex-A9 上无法覆盖的地址、链表和 GDB `gdb.Value` 访问路径。

- 启动方式：M-Mode，`-bios rtthread.bin`、`-m 256M`，不使用 ARM BSP 所需的
  SD 镜像；GDB 加载独立的 `rtthread.elf` 并设置 `riscv:rv64` 架构。
- 版本矩阵：`v4.0.4`、`v4.0.5`、`v4.1.0`、`v4.1.1`。`v4.0.0-v4.0.3` 没有
  上游 RV64 QEMU BSP，不能伪造覆盖。
- BSP 路径：`v4.0.4-v4.1.0` 使用 `bsp/qemu-riscv-virt64`；`v4.1.1` 重命名为
  `bsp/qemu-virt64-riscv`。补丁集按 `4.0.4-4.0.5`、`4.1.0`、`4.1.1` 划分，
  以隔离 newlib 兼容补丁的源文件差异。
- 补丁布局：`ci/rt-thread/patches/cortex-a9/<版本>/` 与
  `ci/rt-thread/patches/rv64/<版本>/` 分层。RV64 只保留 fixture 补丁，编译
  兼容补丁仅在对应工具链确有需要时增加。
- 测试会话需等待 fixture 的 `GDR test fixture ready.` 串口标记，并断言
  `sizeof(void *) == 8`，而非使用固定启动延时。

---

## P0 — 已完成

### ✅ `@gdb_command_guard` 装饰器 + 诊断日志

**文件**: `gdr/gdb_bridge.py`, `rtthread/commands.py`

借鉴 GEF `GenericCommand.invoke` catch-all (`gef.py:5360`) 的轻量版：

- 新增 `is_debug()` 读 `GDR_DEBUG` 环境变量（对应 GEF `gef.debug`）
- 新增 `err(msg)` 与 `warn(msg)` 区分可恢复降级 vs 彻底失败
  （对应 GEF `err()`/`warn()` 五档前缀函数 `gef.py:2076-2099`）
- 新增 `format_exception(e)`：单行摘要，`GDR_DEBUG=1` 时附完整 traceback
  （对应 GEF `show_last_exception` `gef.py:2111`，裁剪掉 noisy 的 GDB
  命令历史部分，对 RTOS 远程调试更克制）
- 新增 `gdb_command_guard` 装饰器：捕获 `(gdb.error, gdb.MemoryError)`
  转 `warn`，其他 `Exception` 转 `err`。RTOS 调试中目标已死/内存不可读
  是常态，避免它们以 GDB "Python Exception" 形式打断流程。
- 装饰 `commands.py` 全部 5 个 `_cmd_*` 函数。

### ✅ 枚举字段符号化

**文件**: `gdr/layout.py`, `gdr/printers.py`, `rtthread/layout.py`

- `StructField` 新增 `enum_map: dict[int, str] | None`
- `_format_field` 改为接收完整 `field` 对象：
  - `kind="enum"` + `enum_map` → 渲染符号名（`stat=SUSPEND` 代替 `stat=2`）
  - `kind="flags"` + `enum_map` → `|` 拼接置位名
    （`flag=ACTIVE|PERIODIC|SOFT` 代替 `flag=0x7`）
- `rtthread/layout.py` 三张映射表，挂在对应字段：
  - `OBJECT_TYPE_NAMES` → 所有 `type` 字段
  - `THREAD_STAT_NAMES` → `rt_thread.stat`（summary）
  - `TIMER_FLAG_NAMES` → `rt_timer.flag`（summary，新增可见）
- 新增 4 个回归测试 `tests/test_printers.py`，24 个测试全绿。

---

## P1 — 高收益改进

### 1.1 read_cstring 跨页兜底（不实施）

**文件**: `gdr/gdb_bridge.py`

GEF 的实现面向 Linux 等 MMU 目标。GDR 调试的是裸机 RTOS，当前名称字段也
是固定 `char[]` 而非跨页 `char*`；分页兜底没有实际收益，避免为不存在的场景
增加复杂度。

### ✅ 1.2 函数指针符号化

**文件**: `gdr/gdb_bridge.py`, `gdr/printers.py`, `tests/test_printers.py`

GEF `dereference_from` (`gef.py:9859`) 对 text 段指针自动 disasm。GDR
`_format_field` 的 `kind="ptr"` 分支当 `deref["name"]` 失败时只回退
`hex(addr)`。现通过 `lookup_symbol_at()` 集中封装 `gdb.execute` 与输出解析，
把 `entry=0x08001234` 渲染为 `entry=<rt_thread_entry+4>`；无匹配符号时保持
原始地址。

### ✅ 1.3 `rtthread threads` 增加 StkUsed 列

**文件**: `rtthread/commands.py`, `gdr/abstractions.py`,
`tests/test_abstractions.py`, `tests/test_commands.py`

RTOS 调试最关心栈溢出。当前只输出 SP/StkSize/Entry，加一列
`StkUsed = stack_size - (sp - stack_addr)`。当保存的 SP 不在栈范围内时显示
`N/A`，避免把损坏或过期的上下文误报为有效使用量。向上增长的架构使用
`sp - stack_addr`；通过 `ARCH_CPU_STACK_GROWS_UPWARD` 宏或 RT-Thread 栈填充
哨兵判断方向，未知时安全降级为 `N/A`。同时扫描 `'#'` 填充区，输出历史最大
使用量 `MaxStkUsed`。

### ✅ 1.4 SMP 当前线程

**文件**: `rtthread/navigation.py`, `tests/test_functions.py`,
`tests/test_rtthread_navigation.py`

RT-Thread 非 SMP 内核使用标量 `rt_current_thread`；SMP 内核把句柄放在
当前 `struct rt_cpu` 的 `current_thread` 字段。实现优先使用 4.x 稳定的
`rt_cpu_index(rt_hw_cpu_id())`，并兼容直接导出的 `rt_cpu_table`、
`rt_cpus`（v4.0.0）和 `_cpus`（v4.0.5+）数组或指针表。

这不能抽象成核心层的统一表结构：FreeRTOS SMP 使用
`pxCurrentTCBs[core]`，Zephyr 使用 `_kernel.cpus[core].current`。各 RTOS
适配器必须按自身的 CPU 选择和线程句柄组织实现导航。Cortex-A9 SMP 闭环测试
断言 GDR 返回的线程与 `rt_cpu_index(rt_hw_cpu_id())->current_thread` 相同。

### ✅ 1.5 print_table 输出隔离

**文件**: `gdr/gdb_bridge.py`, `tests/test_gdb_bridge.py`

GEF `@bufferize` (`gef.py:241`) 把一次命令的输出收进 StringIO 末尾 flush，
避免与 GDB 自身异步消息交错。GDR `print_table` 现在先缓存完整表格，再以一次
`gdb.write()` 输出；空表路径同样遵守该约束。颜色和当前线程行高亮属于独立的
展示策略，暂不引入配置或 ANSI 输出。

### ✅ 1.6 iter_list 截断报警

**文件**: `gdr/layout.py`

GEF 递归 deref 带 `seen_addrs` 循环检测。GDR `iter_list` 只靠
`count < max_count` 默默截断；现通过 `seen_addrs` 检测不经过头节点的损坏环并
`warn(...)`，达到 `max_count` 且尚未回到头节点时同样报警。

### ✅ 1.7 注册接口防重入

**文件**: `gdr.py`, `gdr/printers.py`, `rtthread/adapter.py`,
`rtthread/commands.py`, `tests/test_registration.py`

`gdr init` 是会话级一次性 RTOS 初始化，不是配置重载入口。打印机、便利函数与
命令注册均保留首个布局，重复调用不改变状态；入口直接提示重启 GDB 后再选择其他
目标或版本。打印机 lookup 使用标记防重入，显式开发期注销仍可移除全部 GDR
打印机。

### ✅ 1.8 ArchInfo 轻量架构描述

**文件**: `gdr/gdb_bridge.py`, `tests/test_gdb_bridge.py`,
`tests/test_functions.py`

GEF `Architecture` 基类 + `__init_subclass__` 强制契约 (`gef.py:2624`)。
GDR 不需要那么完备，现提供不缓存的 `get_arch_info() -> ArchInfo | None`：从目标
指针类型获得 `ptrsize`，从 GDB 已解析的 `show endian` 输出获得 `endian`，兼容旧
GDB 的 `void` 类型回退。`read_int` 已由 GDB 按目标字节序解码；`read_bytes` 保留
原始内存顺序，调用方需要解码原始整数时使用 `ArchInfo.endian`。

### 1.9 read_bytes inferior.is_valid() 前置检查

**文件**: `gdr/gdb_bridge.py`

GEF 多个 `gdb.selected_inferior()` 调用点前检查 attached。GDR `read_bytes`
没有；目标 detach 后调用会抛裸异常。

---

## P2 — 锦上添花

### 2.1 MAX_LIST_LEN 可配置

**文件**: `gdr/gdb_bridge.py`

当前硬编码 4096。大内核线程表超限会被默默截断。改为可通过 `gdr.set`
命令或环境变量调整。

### 2.2 README pdb 调试说明

**文件**: `README.md`

GEF 文档明确支持 `pdb.set_trace()` 和 `debugpy` 注入。GDR 在 GDB 内运行
时调试自身很别扭；README 加一段说明显著降低维护成本。

---

## 决策依据 — 平台对比详述

### GitHub Actions 用于 GDR 的优劣

#### GitHub Actions 优势

- 仓库远端已配置，零迁移成本
- `ubuntu-24.04-arm` 提供 GA 级原生 arm64 runner（2025 起）
- 公开仓库无限分钟免费，社区接受度高
- Actions 市场成熟（actions/checkout、setup-python 等）

#### GitHub Actions 劣势

- 国内拉取慢：QEMU + GDB + RT-Thread 源码全在境外镜像，CI 单次
  工具链准备可能 5-10 分钟
- 远程调试入口缺失：流水线失败只能靠日志复现，无法 ssh 进容器
- 自定义镜像缓存需外部 registry（GHCR 限速明显）
- LLMs 文档与中文社区资料相对薄弱

### CNB 用于 GDR 的优劣

#### CNB 优势

- 国内节点：QEMU/RT-Thread 源码拉取快，单次工具链准备 <2 分钟
- `docker.build` 镜像哈希自动缓存到自带制品库 `docker.cnb.cool`，
  二次构建秒级复用；`versionBy` 让依赖变化才重建
- `vscode` service 配 `failStages` 可在测试失败时一键远程登录复现，
  对 GDB 测试 flaky 场景极有用
- 64 核弹性 + copy-on-write 缓存，并发构建无冲突
- 中文文档完整，`cnb` CLI 可本地查知识库

#### CNB 劣势

- 仓库需镜像到 CNB（一次性配置，可 git remote 加 `cnb` 远端）
- 社区规模与 GitHub 不在一个量级（公开仓库可见性影响有限）

### 最终建议

**CI 全部统一到 CNB，GitHub 仅保留代码托管。** 理由：lint 和 qemu-test
在 CNB 一处声明，单一配置源彻底消除"双向同步 lint 规则"的维护成本；
CNB 自带镜像缓存 + vscode 远程调试入口对轻量 lint 与重型 QEMU 测试
**都**有增益。GitHub 仓库可见性退化为代码镜像，badge 用 CNB 的即可。
