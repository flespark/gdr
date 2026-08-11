# GDR FreeRTOS 支持实施计划

> 状态：Phase 1、Phase 2 已完成；Phase 3 待实施
> 调研基线日期：2026-08-10

## 1. 目标与已确认决策

在不破坏现有 RT-Thread 支持的前提下，为 GDR 增加独立的 FreeRTOS
adapter，并完成：

- B-L475E-IOT01A + QEMU 的真实启动和 GDB 闭环；
- FreeRTOS 任务、队列、信号量、互斥量和活动软件定时器的导航；
- `tasks`、`system` 等聚合命令；
- Queue Registry、活动 Timer、Pretty-printer 和 GDB convenience function；
- FreeRTOS 多版本构建与 QEMU 测试矩阵；
- 同时包含 RT-Thread 和 FreeRTOS 的统一发布包；
- 英文文档、中文文档和架构说明；
- 对 FreeRTOS 结构变化、配置变化和枚举能力边界的明确支持声明。

首批版本优先级固定为：

| 优先级 | 主基线 | 首批支持跨度 |
|---|---|---|
| P1 | `V10.3.1-kernel-only` | `V10.3.0` 至 `V10.3.1` |
| P2 | `V10.6.2` | `V10.5.0` 至 `V10.6.2` |
| P3 | `V11.1.0` | 首期 `V11.0.0` 至 `V11.1.0`，后续扩展到 `V11.3.0` |

FreeRTOS Kernel 仓库中没有标准的 `V10.3.1` tag，实际 tag 是
`V10.3.1-kernel-only`。CI 和构建脚本必须使用真实 tag 或固定 commit，
不能假定 `V10.3.1` tag 存在。

基础任务、队列、信号量、互斥量、定时器和事件组字段在 `V10.3.1`
至 `V11.3.0` 间总体稳定。版本差异应通过 DWARF 字段路径、字段存在性
和目标类型信息处理，禁止硬编码字节偏移。

FreeRTOS 不提供 RT-Thread 式统一对象注册表，因此功能语义固定为：

- Task：可从调度器链表完整枚举；
- Queue/Semaphore/Mutex：只能保证枚举已加入 Queue Registry 的对象；
- Timer：只能保证枚举当前处于 active timer list 的软件定时器；
- 未注册 Queue、已删除 Timer、尚未加入 active list 的 Timer 不承诺可见。

初期 QEMU 闭环限定为单核 Cortex-M。FreeRTOS 11 SMP 先完成 layout 和
编译覆盖，暂不宣称 QEMU 多核运行验证。

## 2. 调研结论

### 2.1 QEMU 与板卡

首选板卡为 ST B-L475E-IOT01A：

- QEMU machine：`b-l475e-iot01a`；
- CPU：Cortex-M4F，单核；
- SDK：STM32CubeL4 `v1.18.2`；
- SDK 中 FreeRTOS 子模块 commit：
  `5fe3a380e5eadb6ce0a5149725210c3fe70d1c15`；
- 内核版本：ST 修改版 FreeRTOS `V10.3.1`；
- QEMU 官方文档：
  `https://www.qemu.org/docs/master/system/arm/b-l475e-iot01a.html`；
- ST 官方示例：
  `STM32CubeL4/Projects/B-L475E-IOT01A/Applications/FreeRTOS`；
- QEMU 当前未实现 LPTIM，fixture 必须使用 Cortex-M SysTick 作为
  FreeRTOS tick，并通过 USART 或 semihosting 输出启动标记。

第二候选为 AMD/Xilinx Zynq-7000：

- QEMU machine：`xilinx-zynq-a9`；
- SDK：Vitis/`embeddedsw`；
- 存在 `ThirdParty/bsp/freertos10_xilinx`；
- BSP 生成和 Vitis 工具链依赖更重，不作为首个闭环目标。

### 2.2 SDK 与 FreeRTOS 版本证据

| 厂商/SDK | 代表芯片 | 当前 SDK 线索 | FreeRTOS 证据与结论 |
|---|---|---|---|
| ST STM32CubeF1/F4/G4/H7/L4 | STM32 Cortex-M | 最新 tag 分别为 `v1.8.7`、`v1.28.3`、`v1.6.3`、`v1.13.0`、`v1.18.2` | 长期固化 ST FreeRTOS `V10.3.1` |
| ST STM32CubeWB | STM32WB | `v1.24.0` | 内置 FreeRTOS `V10.6.2` |
| ST STM32CubeU5 | STM32U5 | `v1.9.0` | 最新包未在同等位置提供 FreeRTOS Kernel，不作为首个目标 |
| Infineon FreeRTOS fork | PSoC/XMC 等 | `release-v10.6.202` | `task.h` 声明 `V10.6.2` |
| Renesas FSP | RA 系列 | `v6.5.1` | 包含 FreeRTOS port 和 FreeRTOS+ 组件，但 Kernel 通常由独立包管理，不能只凭 FSP 主仓库断言固定 tag |
| Microchip Harmony 3 | SAM/PIC32 | Harmony core `v3.17.0` | 提供 FreeRTOS 配置器和 port，Kernel 版本由独立组件包管理 |
| NXP MCUXpresso SDK | i.MX RT/LPC/Kinetis/MCX | `MCUX_2.16.100` 及 Real-Time Edge 日历 tag | Kernel 版本随具体 SDK manifest/包发布，不能把 SDK tag 直接视为 FreeRTOS tag |
| TI SimpleLink | CC13xx/CC26xx/MSP432 等 | SimpleLink SDK 分支 | Kernel 由具体 SDK/组件提供，需按产品 SDK 固化版本 |
| Raspberry Pi Pico SDK | RP2040/RP2350 | `2.3.0` | Pico SDK 不等于固定 vendored FreeRTOS Kernel，port/Kernel 多由独立组件提供 |
| AMD/Xilinx Vitis | Zynq/ZynqMP/MicroBlaze | `embeddedsw` | 有 `freertos10_xilinx` BSP，版本受 Vitis/BSP 生成流程影响 |

### 2.3 出货量证据口径

公开资料大多不能精确拆到芯片家族，因此文档和后续调研记录必须区分
“芯片出货量”和“公司营收/市场排名”：

- ST：公开资料通常给出 STM32 累计出货量达到百亿级，但未按
  F1/F4/G4/H7/L4/WB/U5 拆分年度颗数；STM32 生态和 SDK 覆盖面评为极高；
- Renesas：公开资料曾给出公司级 MCU 年出货约数十亿颗量级，但不能
  直接等价为 RA 系列出货量；
- Microchip：公开的数十亿或数百亿级数字多为公司级或累计口径，不能
  拆成 SAM/PIC32 家族年度颗数；
- NXP、Infineon、TI、AMD/Xilinx：家族级 FreeRTOS MCU/SoC 颗数通常
  未公开，应使用公司级数据、产品覆盖和 SDK 使用面作为定性权重；
- Raspberry Pi：RP2040/Pico 有公开销售和累计数量线索，但必须标明
  统计对象是 Raspberry Pi 产品或 RP2040，而不是整个 MCU 市场；
- 不得使用营收数字冒充出货量。每条数据必须记录统计对象、年份、
  是否累计、是否公司级、原始证据链接和证据等级。

这些证据支持以下版本选择：

- `V10.3.1` 覆盖 STM32 主流历史装机和大量长期维护 SDK；
- `V10.6.x` 覆盖较新的 ST/Infineon 生态，并覆盖 10.5.x 结构代际；
- `V11.x` 作为现代主线和 SMP 前瞻基线，优先验证 `V11.1.0`。

### 2.4 内核结构兼容性结论

#### V10.3.1 至 V10.4.0

TCB 中任务通知由标量变为数组：

```c
/* V10.3.1 */
volatile uint32_t ulNotifiedValue;
volatile uint8_t ucNotifyState;

/* V10.4.0+ */
volatile uint32_t ulNotifiedValue[ configTASK_NOTIFICATION_ARRAY_ENTRIES ];
volatile uint8_t ucNotifyState[ configTASK_NOTIFICATION_ARRAY_ENTRIES ];
```

默认数组长度为 1，默认物理尺寸通常不变，但 GDB 字段类型从标量变为
数组。若 GDR 展示通知值，必须处理标量和数组两种访问形式。

#### V10.4.5

`TCB_t.ulRunTimeCounter` 和 `TaskStatus_t.ulRunTimeCounter` 从固定
`uint32_t` 改为 `configRUN_TIME_COUNTER_TYPE`。默认仍是 `uint32_t`，
但应用可设为 64 位并移动后续字段偏移。GDR 必须使用目标 DWARF 类型。

#### V10.5.0

- TCB 的可选 Newlib 字段由 `xNewLib_reent` 泛化为 `xTLSBlock`；
- 新增 `configUSE_MINI_LIST_ITEM`；
- StreamBuffer 可选增加实例级 send/receive callback 字段；
- `TaskStatus_t` 可选增加 stack top/end 信息。

`configUSE_MINI_LIST_ITEM` 默认是 1。设为 0 时 `MiniListItem_t` 直接成为
`ListItem_t`，使 `List_t.xListEnd` 变大，但链表遍历需要的字段路径保持
不变。

#### V10.5.0 至 V10.6.2

`Queue_t`、`Timer_t`、`EventGroup_t` 和调度链表没有目标字段级变化；
`TCB_t` 公共字段保持一致。`V10.6.0` 的风险主要来自配置：

- `configTICK_TYPE_WIDTH_IN_BITS` 允许 16/32/64 位 tick；
- MPU wrapper v2 引入端口相关 `xMPU_SETTINGS` 和用户态 opaque handle；
- Newlib/Picolibc TLS 配置方式调整，但最终 TCB 仍使用 `xTLSBlock`。

因此 `V10.5.x` 与 `V10.6.x` 共用主 profile；MPU v2 作为独立配置覆盖。

#### V10.6.2 至 V11.0.0

SMP 合入主线，多核配置下 TCB 增加：

```c
uxCoreAffinityMask
xTaskRunState
uxTaskAttributes
xPreemptionDisable
```

当前任务入口从 `pxCurrentTCB` 变为
`pxCurrentTCBs[ configNUMBER_OF_CORES ]`。当 `configNUMBER_OF_CORES == 1`
时，SMP 字段不会进入 TCB，仍导出 `pxCurrentTCB`。

#### V11.0.0 至 V11.0.1

没有目标内核对象结构变化，官方 changelog 只有 SBOM 更新。两个版本
应位于同一支持区间。

#### V11.0.1 至 V11.1.0

`TCB_t`、`Queue_t`、`Timer_t`、`EventGroup_t` 不变；`StreamBuffer_t`
末尾新增 `UBaseType_t uxNotificationIndex`。若支持 StreamBuffer，需区分
`pre-11.1` 和 `11.1+` layout。

#### V11.1.0 至 V11.2.0

目标结构不变，但 Queue Set 类型码由与普通 Queue 共用的 `0` 改为独立
值 `5`。如果 GDR 根据 `ucQueueType` 分类对象，`V11.2+` 必须使用新映射。

#### V11.2.0 至 V11.3.0

首期目标对象没有新增字段。加入 StreamBuffer 和 Queue Set 语义分支后，
同一 V11 profile 可以延伸至 `V11.3.0`。

## 3. Phase 1：板卡启动与通用 QEMU Harness

### 3.1 目标

先让 FreeRTOS fixture 在真实 QEMU machine 上启动，并建立与现有
RT-Thread 测试等价的持久 GDB/QEMU 闭环。该阶段不实现 FreeRTOS 对象
命令。

### 3.2 代码和目录结构

- 新增 `freertos/` adapter 包骨架；
- 新增 `ci/freertos/`，存放构建脚本、fixture source/config 和版本补丁；
- 新增 FreeRTOS profile 数据模块，定义 target、version、fixture 期望；
- 将现有 `tests/conftest.py` 中 QEMU/GDB 生命周期抽成 RTOS 无关 harness；
- 保留 RT-Thread profile 和行为，禁止 FreeRTOS 分支污染现有测试。

建议目标结构：

```text
freertos/
  __init__.py

ci/freertos/
  build-freertos.sh
  run-qemu-matrix.sh
  fixture/
  patches/

tests/
  qemu_harness.py
  freertos_profiles.py
```

### 3.3 Fixture 与启动流程

- 使用 STM32CubeL4 B-L475E-IOT01A startup、linker script、HAL/BSP 和
  Cortex-M4 FreeRTOS port；
- 使用 SysTick 产生 FreeRTOS tick，不使用 LPTIM；
- 使用 USART 或 semihosting 输出 `GDR FreeRTOS fixture ready.`；
- marker 只能在所有测试对象创建并完成注册后输出；
- 使用 `-Og -g3`，禁用 LTO，保留局部静态变量和 DWARF；
- ELF 用于 GDB 符号，QEMU firmware image 可与 ELF 分开配置；
- fixture 最少创建三个不同优先级 Task、普通 Queue、Semaphore、Mutex、
  已注册 Queue、未注册 Queue、active Timer 和未启动 Timer。

QEMU/GDB 生命周期固定为：

1. 分配空闲 GDB TCP 端口，避免固定 `1234` 导致并发冲突；
2. 启动 `qemu-system-arm -M b-l475e-iot01a`；
3. 将 serial 输出写入每个测试 session 独立的临时文件；
4. 等待 ready marker，并在超时中打印 serial 和 QEMU 退出状态；
5. 启动持久 GDB session；
6. 设置 architecture、加载 ELF、连接 remote、`source gdr.py`；
7. 所有测试复用该 GDB session；
8. 测试结束后优雅关闭 GDB/QEMU，超时后再强制终止。

### 3.4 公共 Harness 接口

profile 至少提供：

```text
rtos
version
qemu_binary
machine
cpu/extra_args
gdb_architecture
elf_path
firmware_path
firmware_option
ready_marker
pointer_width
```

环境变量统一使用：

```text
GDR_RTOS
GDR_VERSION
GDR_QEMU_TARGET
GDR_QEMU
GDR_QEMU_MACHINE
GDR_GDB
GDR_ELF_PATH
GDR_FIRMWARE_PATH
GDR_BOOT_WAIT
```

保留现有 RT-Thread 兼容变量一个发布周期，再逐步迁移到统一变量。

### 3.5 测试与验收

- QEMU 能稳定启动，不依赖真实硬件；
- GDB 能读取 `tskTaskControlBlock`、`QueueDefinition`、`tmrTimerControl`；
- 持久 GDB session 能连续执行多条表达式；
- `source gdr.py` 不报错；
- 测试能区分 fixture 超时、QEMU 提前退出、GDB 连接失败和缺少工具；
- ARM pointer width 断言为 4；
- FreeRTOS 闭环测试和 RT-Thread 原有闭环测试同时通过。

### 3.6 实施结果（2026-08-10）

- 已新增 `freertos/` 包骨架、`ci/freertos/` fixture/build runner 和
  独立的 `tests/freertos_profiles.py`；生产 adapter 尚未进入导航或命令实现。
- `tests/qemu_harness.py` 已提取 RTOS 无关的 profile、动态 GDB port、
  QEMU 进程日志、启动 marker 和持久 GDB 生命周期。RT-Thread 的 A9/RV64
  profile 保留旧变量兼容；A9 fixture 不依赖 SD device 启动。
- `ci/freertos/build-freertos.sh` 使用 STM32CubeL4 `v1.18.2`、其固定的
  CMSIS device/FreeRTOS submodule commit，以及 Cortex-M4F SysTick port 构建
  含 DWARF 的 ELF/BIN。fixture 创建三种优先级 Task、已注册/未注册 Queue、
  Semaphore、Mutex、active/inactive Timer；ready marker 仅在 timer daemon
  已处理 active timer 后经 QEMU semihosting 输出。
- 已在 `qemu-system-arm -M b-l475e-iot01a` 上验证 marker、持久 GDB、
  `struct tskTaskControlBlock`、`struct QueueDefinition`、
  `struct tmrTimerControl` 与 32-bit pointer width。验证命令：
  `bash ci/freertos/run-qemu-matrix.sh`。

## 4. Phase 2：任务导航、Layout、RTOS 命令树

### 4.1 目标

先完成 FreeRTOS 最稳定、价值最高的任务导航和系统摘要。

### 4.2 版本和配置模块

新增 `freertos/version.py`：

- 解析完整三段版本，例如 `10.3.1`；
- 首期接受 `10.3.0-10.3.1`、`10.5.0-10.6.2`、
  `11.0.0-11.1.0`；
- 保留对 `10.4.x` 和 `11.2-11.3` 的内部 build/layout profile；
- 对 unsupported version 给出明确支持范围；
- 不使用移动的 `main` 作为兼容基线；
- 当目标导出版本常量时比较目标版本，否则只告警，不猜测 RTOS。

新增 `freertos/config.py`，通过 GDB symbol/type 探测：

- 单核或 SMP；
- Task notification 标量或数组及数组长度；
- TickType 宽度；
- runtime counter 宽度；
- MiniList 开关；
- TLS 字段；
- stack growth 和 stack end 字段；
- trace facility、Queue Registry、Timer、静态分配和 MPU wrapper v2。

### 4.3 Layout

新增 `freertos/layout.py`，作为 FreeRTOS 结构耦合的唯一 owner：

- 描述 `tskTaskControlBlock/TCB_t`；
- 描述 `List_t`、`ListItem_t`、`MiniListItem_t`；
- 描述 `TaskStatus_t`；
- 预留 Queue、Timer、EventGroup、StreamBuffer layout；
- 所有字段使用 DWARF path，不保存固定 offset；
- 条件字段由配置 factory 组合；
- 仅在通知标量/数组、V11 SMP 和 Queue type code 等真实边界上分支。

### 4.4 Task 导航

新增 `freertos/navigation.py`：

- 遍历 `pxReadyTasksLists[ configMAX_PRIORITIES ]`；
- 遍历 `xDelayedTaskList1`、`xDelayedTaskList2`；
- 识别 `pxDelayedTaskList` 和 `pxOverflowDelayedTaskList`；
- 遍历 `xPendingReadyList`；
- 按配置遍历 `xSuspendedTaskList`；
- 按配置遍历 `xTasksWaitingTermination`；
- 从 `xStateListItem.pvOwner` 获取 TCB；
- 以目标地址去重，防止同一 Task 重复输出；
- 单核读取 `pxCurrentTCB`；
- SMP 读取 `pxCurrentTCBs[]`，但首期只做 layout/build 验证；
- 根据所在 list、current TCB 和 SMP run state 映射 Running、Ready、
  Blocked、Suspended、Deleted/Pending 状态；
- 单个链表损坏时停止该链表遍历并警告，禁止无限循环或拖死 GDB。

### 4.5 Adapter 和 Commands

新增 `freertos/adapter.py` 的任务转换逻辑：

- name；
- state；
- priority/base priority；
- top-of-stack、stack base/end；
- stack size/used/high-water mark；
- runtime counter；
- entry function；
- current/core marker。

入口和 RTOS 专属命令固定为：

```gdb
gdr init freertos 10.3.1
freertos tasks
freertos system
```

`rtthread` / `rtt` 保留其命令树；其中 `threads`、`semaphores`、`mutexes`、
`timers`、`messagequeues`、`mailboxs` 和 `system` 直接使用 RT-Thread
adapter 的能力，并提供 `tasks`、`sems`、`mtxs`、`msgs`、`mboxs` 短别名。
`rtt objects` 不再注册。共享渲染器可以位于 `gdr/`，但不注册为 `gdr` 的
数据打印子命令。
`rtt system` 至少输出：

- Kernel version；
- 当前 Task；
- Task 总数；
- Tick count；
- Scheduler state；
- Ready/Delayed/Pending/Suspended/Termination 数量；
- adapter 可可靠枚举的对象计数；
- 能可靠读取时输出 heap summary，否则显示 unavailable，不猜测值。

### 4.6 测试与验收

- `freertos tasks` / `rtt threads` 列出所有 fixture Task；
- 状态分类和当前任务标记正确；
- 多链表重复 Task 不重复显示；
- 空/损坏链表不会无限遍历；
- RTOS 专属 `system` 命令的 tick、current task、task count 与原生 GDB 表达式一致；
- `V10.3.1`、`V10.6.2` 单元/layout 测试通过；
- `V10.4.0` 通知数组边界有 build/layout 覆盖；
- SMP 配置完成 compile/layout test，但文档不宣称 QEMU runtime 支持。

### 4.7 实施结果（2026-08-10）

- 已新增 `freertos/version.py`、`config.py`、`layout.py`、`navigation.py` 和
  `adapter.py`；版本检查不使用移动的 `main`，目标未导出
  版本常量时只告警。
- `gdr init freertos <version>` 已接入入口；`freertos tasks` 使用
  ready/delayed/pending/suspended/termination 链表的
  DWARF 字段路径遍历，并按 TCB 目标地址去重、限制损坏链表遍历长度。
- `/workspace/ref/freertos` 作为结构和配置参考；V10.3.1 fixture 的真实
  QEMU/GDB 回归验证 `3 passed`，任务、当前任务、tick、调度器和链表计数均
  与目标原生值一致。Heap 在无法可靠读取时显示 `unavailable`。

### 4.8 统一语义 API 重构（已完成）

- `gdr/adapter_api.py`、`registry.py`、`functions.py` 和 `commands.py` 定义
  只含语义的 active-adapter 协议、内部渲染器和 raw-value 函数。公开 task
  函数固定为 `$gdr_task(name)` 和 `$gdr_tasks()`；`gdr` 公共命令只保留
  `gdr init` 与 `gdr help`。返回值仍是目标原生 `gdb.Value`，不伪造统一 C 结构。
- `$gdr_object(kind, name)` 统一使用小写语义种类。RT-Thread 可通过对象注册表
  提供 task、semaphore、mutex、timer 等；FreeRTOS 在 Phase 2 仅声明 task，
  其余种类在实现可靠枚举前返回 unavailable/null，不能把不可见对象误报为空。
- RT-Thread 保留 `rtthread` / `rtt` 命令树，提供 `threads`、`semaphores`、
  `mutexes`、`timers`、`messagequeues`、`mailboxs` 和 `system`，并提供
  `tasks`、`sems`、`mtxs`、`msgs`、`mboxs` 短别名；删除冗余的
  `rtt objects`。真实 QEMU 验证通过 `rtt timers` 检查完整定时器表。

## 5. Phase 3：Queue Registry、活动 Timer、Pretty-printer 和便捷函数

### 5.1 Queue Registry

- 读取 `xQueueRegistry[ configQUEUE_REGISTRY_SIZE ]`；
- 读取 `pcQueueName` 和 `xHandle`；
- 跳过空 handle、空名称和已注销条目；
- 按地址去重；
- 不扫描 heap 推测未注册 Queue；
- Registry 未启用时命令显示明确提示，而不是返回虚假的空系统。

扩展 RTOS 专属对象命令：

```gdb
freertos queues
freertos semaphores
freertos mutexes
freertos timers
```

### 5.2 Queue 和同步对象分类

- trace facility 可用时读取 `ucQueueType`；
- `10.x/11.0-11.1` 使用 Queue Set 值 `0`；
- `11.2+` 使用 Queue Set 值 `5`；
- 结合 `uxItemSize`、mutex holder、recursive count 等字段做安全 fallback；
- 无法可靠区分时输出 `unknown`，不得猜测 Queue 类型；
- Queue 行至少显示 name、type、length、item size、messages waiting、address；
- Semaphore 显示 count/max count；
- Mutex 显示 owner、recursive count、waiting task 数量。

### 5.3 活动 Timer

- 读取 `xActiveTimerList1` 和 `xActiveTimerList2`；
- 读取 `pxCurrentTimerList` 和 `pxOverflowTimerList`；
- 从 `Timer_t.xTimerListItem.pvOwner` 获取 Timer；
- 展示 name、period、expiry tick、auto-reload、active 状态、callback；
- callback 使用目标符号解析，同时保留原始地址 fallback；
- 未启动、已删除、已过期并移出 active list 的 Timer 不保证可见。

### 5.4 Pretty-printer

新增目标类型摘要：

- TCB/Task；
- Queue；
- Semaphore；
- Mutex；
- Timer；
- EventGroup（目标启用且字段可读时）。

只注册实际存在的 DWARF 类型。任一类型解析失败必须回退到 GDB 默认显示，
不能导致 `source gdr.py` 或其他对象 printer 失效。

### 5.5 Convenience Functions

固定公共接口为：

```gdb
$gdr_task(name)
$gdr_tasks()
$gdr_object("queue", name)
$gdr_object("timer", name)
```

- 返回原始 `gdb.Value` 或 GDB 原生指针数组；
- 不建立第二套 Python 内核对象模型；
- `$gdr_object("queue", ...)` 仅查 Queue Registry；
- `$gdr_object("timer", ...)` 仅查 active timer list；
- 查找失败返回 null/void 值并输出一致的错误信息；
- 用户可继续用 `p`、字段访问、watch 和脚本处理返回值。

### 5.6 测试与验收

- 注册 Queue 可按名称检索；
- 未注册 Queue 不被错误列入完整对象清单；
- Queue、binary/counting semaphore、mutex 正确分类或明确标为 unknown；
- active Timer 显示 callback 符号和 tick 信息；
- 未启动 Timer 不被误报为 active；
- Pretty-printer 在 `10.3.1`、`10.6.2`、`11.1.0` 输出稳定摘要；
- convenience function 返回可继续用于 GDB 表达式的原始值；
- fake tests 和 QEMU tests 同时覆盖成功、空集合、unknown type、
  registry disabled、trace disabled 和损坏链表。

## 6. Phase 4：完整矩阵、统一发布包和中英文文档

### 6.1 版本矩阵

Full QEMU 闭环：

```text
V10.3.1-kernel-only
V10.6.2
V11.1.0
```

快速 build/layout 覆盖：

```text
V10.3.0-kernel-only  # 10.3 下界
V10.4.0-kernel-only  # notification array 边界
V10.4.5              # configurable runtime counter 边界
V10.5.0              # TLS/MiniList/StreamBuffer callback 边界
V10.6.0              # tick width 和 MPU v2 边界
V11.0.1              # SMP 主线稳定基线
V11.2.0              # Queue Set type code 边界
V11.3.0              # 最新稳定上界
```

配置矩阵：

- notification array count 1/3；
- runtime counter 32/64 bit；
- MiniList 1/0；
- tick width 16/32/64；
- Queue Registry enabled/disabled；
- trace facility enabled/disabled；
- StreamBuffer callback enabled；
- V11 `configNUMBER_OF_CORES=2` compile/layout；
- 普通 port 和 MPU wrapper v2 compile/layout。

### 6.2 CI

- CNB `.cnb.yml` 增加 FreeRTOS build、QEMU、matrix job；
- GitHub Actions 保留快速 Python/unit 检查，并增加可控 FreeRTOS smoke job；
- 复用现有 QEMU/GDB/ARM GCC Docker image；
- 固定 QEMU、GDB、GCC、SDK、FreeRTOS tag 和 commit；
- 不从移动 `main` 构建 release；
- cache key 包含 RTOS、tag、target、toolchain 和 fixture config hash；
- CI 失败输出 Kernel tag/commit、compiler、QEMU、GDB、fixture config、
  serial log 和 GDB transcript；
- RT-Thread A9、RV64 和 GDB 12 兼容任务保持原有覆盖。

### 6.3 统一发布包

- 更新 `ci/create-release-archives.sh`，统一打包 `gdr/`、`rtthread/`、
  `freertos/` 和单个 `gdr.py`；
- 包内增加支持矩阵和版本 manifest；
- 不携带完整 FreeRTOS/STM32Cube 源码，只携带 GDR adapter 和验证元数据；
- archive 内容测试必须从临时目录加载，不依赖仓库 checkout；
- 保证 GDB 12/Python 3.10 最低兼容基线；
- release notes 明确新增 FreeRTOS、支持 tag、QEMU target 和已知限制。

### 6.4 文档

更新：

- `README.md`；
- `README.zh-CN.md`；
- `docs/architecture.md`；
- `CHANGELOG.md`；
- 本 `PLAN.md` 的状态和实施结果。

中英文文档必须同步包含：

- `gdr init freertos <version>`；
- `freertos tasks`、`freertos system` 及后续对象命令；
- 全部 commands 和 convenience functions；
- 支持版本表；
- B-L475E-IOT01A QEMU 复现方法；
- SDK/Kernel 固定版本和证据链接；
- Task 完整枚举、Queue Registry 限定、active Timer 限定；
- 单核 V11 runtime 与 SMP compile/layout 的区别；
- unsupported configuration 和错误诊断方法。

### 6.5 Phase 4 验收标准

- 三个 Full QEMU tag 在 CI 中稳定通过；
- build/layout 矩阵覆盖所有已识别结构边界；
- RT-Thread 原有矩阵无回归；
- `uv run pytest tests/ -v`、ruff check、ruff format check、pre-commit 全部通过；
- release archive 同时包含两个 RTOS adapter 并可从干净目录加载；
- 英文和中文文档的命令、版本跨度、限制和示例一致；
- 每个矩阵失败可定位到 Kernel tag、配置、target、toolchain 和 GDB 输出。

## 7. 测试组织

建议新增：

```text
tests/test_freertos_version.py
tests/test_freertos_config.py
tests/test_freertos_layout.py
tests/test_freertos_navigation.py
tests/test_freertos_commands.py
tests/test_freertos_printers.py
tests/test_freertos_functions.py
tests/freertos_profiles.py
```

测试原则：

- 不得删除现有测试用例；若测试行为或接口发生变化，必须保留原有覆盖并更新断言或补充新用例。
- 无 GDB 单元测试使用 fake `gdb.Value`/layout；
- QEMU tests 只断言用户可观察行为；
- fixture 期望值独立维护，禁止从生产 layout 自动生成测试期望；
- 每个 layout-sensitive 字段至少有一个字段读取断言；
- 每个版本语义映射至少有一个边界测试；
- QEMU 测试复用持久 GDB session；
- 测试失败必须打印完整命令输出，而不是只显示布尔断言。

实施前基线为现有单元测试 `46 passed`。每个 Phase 完成时都应记录新的
测试数量、QEMU target 和通过的 Kernel tag。

## 8. 维护原则

- `freertos/layout.py` 是 FreeRTOS 结构和配置差异的唯一 owner；
- `freertos/navigation.py` 是 FreeRTOS 全局符号、链表和对象枚举的唯一 owner；
- `freertos/version.py` 只维护版本范围和少量纯语义映射，不复制结构；
- `gdr/` core 不包含 FreeRTOS 或 RT-Thread 类型名；
- 每次 Kernel 字段变化必须更新 layout 单元测试和 QEMU/build fixture；
- 每次支持新配置宏必须增加 compile/layout test；
- 不扫描任意 heap 内存推测未注册 Queue 或 Timer；
- 不加入 RTOS 自动检测；用户显式执行 `gdr init freertos <version>`；
- 不将移动的 FreeRTOS `main` 声明为稳定兼容版本；
- ESP32 不纳入首批调研、板卡、fixture 和 CI 矩阵。

## 9. 首期交付后的支持声明

首期正式文档声明：

```text
FreeRTOS:
  V10.3.0 - V10.3.1
  V10.5.0 - V10.6.2
  V11.0.0 - V11.1.0

QEMU closed-loop:
  B-L475E-IOT01A / b-l475e-iot01a
  V10.3.1-kernel-only
  V10.6.2
  V11.1.0

FreeRTOS 11 SMP:
  layout/build coverage only
  QEMU multi-core runtime verification pending
```

在 `V11.2.0/V11.3.0` 的 Queue Set 类型映射、StreamBuffer 和 build/layout
测试完成后，再将正式支持声明扩展到 `V11.3.0`。

## 10. Phase 完成门槛

每个 Phase 只有满足以下条件才能进入下一阶段：

1. 该 Phase 的全部验收标准通过；
2. 新增代码具有不依赖 GDB 的单元测试和必要的 QEMU smoke test；
3. RT-Thread 现有功能无回归；
4. ruff、format、pytest 和相关 CI 脚本通过；
5. 架构或公开接口变化已同步到中英文文档草稿；
6. 已知限制被明确记录，不使用“后续处理”掩盖当前错误行为。
