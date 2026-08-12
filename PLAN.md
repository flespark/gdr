## 11. RT-Thread 内核对象打印完整性补强

> 状态：待实施
> 调研基线日期：2026-08-11
> 支持范围：RT-Thread `v3.1.0-v3.1.5`、`v4.0.0-v4.0.5`、
> `v4.1.0-v4.1.1`

### 11.1 目标和边界

补齐 RT-Thread 聚合命令中对现场诊断有直接价值、但当前尚未显示的字段，
重点覆盖 IPC 阻塞关系、优先级继承、容量耗尽、事件等待条件、定时器剩余
时间和 SMP CPU 归属。

实施必须遵守以下边界：

- 聚合表只显示可快速判断系统状态的摘要，不把内部链表节点和裸指针全部
  塞入默认输出；
- `$gdr_object()` 和 `$gdr_task()` 继续返回目标原生 `gdb.Value`，允许用户
  使用 GDB 表达式检查任意底层字段；
- 结构字段从目标 DWARF 和 layout 读取，禁止硬编码字节偏移；
- 缺少某字段的旧版本必须显示 `N/A` 或省略对应能力，不能把“不支持”
  错报为数值 `0`；
- 等待线程数量统一通过链表遍历计算，不依赖已经被后续版本删除的缓存
  count 字段；
- 链表遍历必须有损坏检测和最大节点数限制，不能让 GDB 因坏链表死循环；
- 保持 RTOS-neutral core 与 RT-Thread adapter 的职责边界，RT-Thread 结构名、
  字段路径和版本条件只能位于 `rtthread/`。

### 11.2 Messagequeue 与 Mailbox 字段结论

在全部目标版本中，`struct rt_messagequeue` 都不存在 `in_offset` 和
`out_offset`。这两个字段属于 `struct rt_mailbox`。

Mailbox 使用环形数组：

- `msg_pool` 是消息槽数组；
- `in_offset` 指向下一次写入位置；
- `out_offset` 指向下一条待取消息；
- `entry` 是当前消息数量，`size` 是总槽位数。

GDR 已读取并打印 mailbox 的 `entry/size/in_offset/out_offset`。对默认汇总
而言，`entry/size` 已足够判断空满；offset 对恢复实际 FIFO 顺序和诊断
环形游标损坏仍有价值，因此保留现有列。

Messagequeue 使用消息链表和空闲链表：

```c
void *msg_queue_head;
void *msg_queue_tail;
void *msg_queue_free;
rt_uint16_t entry;
```

发送时从 `msg_queue_free` 取节点并挂入 `head/tail`，接收时从 `head`
摘除节点并放回 free list。当前默认表中的 `entry/msg_size/max_msgs` 已足以
判断队列负载、容量和单条消息大小，不得为 messagequeue 添加虚构的 offset
字段。

`msg_queue_head/tail/free` 对检查链表损坏、节点泄漏和消息内容有用，但应由
原生 `$gdr_object()` 钻取或后续 detail/traversal 命令呈现，不作为默认表
的裸指针列。

### 11.3 打印缺口和字段分类

RT-Thread 自带的 `list_sem/list_event/list_mutex/list_mailbox/list_msgqueue/`
`list_mempool` 都把挂起线程数量和名称作为核心诊断信息。GDR 当前这些聚合
表均未显示等待关系，这是优先级最高的缺口。

字段分类固定为：

- **关键字段**：对象列表必须保留，不因终端宽度自动删除。弹性字段中的关键
  前缀不可丢失，例如 `Waiters` 中的等待数量；
- **非关键单值字段**：按 120 字符终端预算评估。能够通过限制弹性文本宽度
  容纳的，固定保留在紧凑列表中，不随实际终端宽度动态增删；
- **非关键详情字段**：内部指针、可变数量的子项、等待条件和一致性检查，
  只在 `rtt <object> <obj_name>` 或原生 `$gdr_object()` 中显示。

列表字段集合必须稳定。实际终端宽度只影响弹性文本是否截断，不影响列是否
存在。

| 对象 | 新增字段 | 分类 | 紧凑列表决定 | Detail 补充 |
|---|---|---|---|---|
| Thread | `Addr` | 关键 | 固定增加 | 原始对象类型和地址 |
| Thread | `BasePrio` | 非关键单值 | 120 列预算内保留 | 当前/基础优先级解释 |
| Thread | `CPU` | SMP 下关键 | SMP 目标固定增加 | CPU sentinel 原始值 |
| Thread | `Bind` | 非关键单值 | SMP 目标在 120 列预算内保留 | 绑定策略和 sentinel |
| Thread | `Error/Remain` | 非关键详情 | 不进入列表 | 错误码和剩余时间片 |
| Timer | `Addr` | 关键 | 固定增加 | 原始 timer 地址 |
| Timer | `ExpiresIn` | 关键状态 | 固定增加，inactive 显示 `N/A` | tick 计算和回绕信息 |
| Timer | `Parameter` | 非关键详情 | 不进入列表 | callback 参数指针 |
| Semaphore | waiter count | 关键 | 固定增加到 `Waiters` | 完整等待线程对象 |
| Semaphore | waiter names | 非关键弹性文本 | 保留，可截断 | 完整名称列表 |
| Semaphore | `Policy` | 非关键单值 | 120 列预算内保留 | 原始 IPC flag |
| Mutex | waiter count | 关键 | 固定增加到 `Waiters` | 完整等待线程对象 |
| Mutex | waiter names | 非关键弹性文本 | 保留，可截断 | 完整名称列表 |
| Mutex | `OriginalPrio` | 关键状态 | 固定增加 | owner 当前/原始优先级关联 |
| Mutex | `Policy` | 非关键单值 | 120 列预算内保留 | 旧版可配置、新版强制 PRIO 的差异 |
| Event | waiter count | 关键 | 固定增加到 `Waiters` | 完整等待线程对象 |
| Event | waiter names | 非关键弹性文本 | 保留，可截断 | 每个 waiter 的完整名称 |
| Event | `Policy` | 非关键单值 | 120 列预算内保留 | 原始 IPC flag |
| Event | `event_set/event_info` | 非关键详情 | 不进入列表 | 每个 waiter 的 mask 和 AND/OR/CLEAR |
| Mailbox | receiver/sender waiter count | 关键 | 固定增加 | 完整 RX/TX 等待线程对象 |
| Mailbox | receiver/sender names | 非关键弹性文本 | 保留，可截断 | 完整 RX/TX 名称列表 |
| Mailbox | `Free` | 非关键单值 | 120 列预算内保留 | `size-entry` 一致性 |
| Mailbox | `Policy` | 非关键单值 | 120 列预算内保留 | 原始 IPC flag |
| Mailbox | `msg_pool` 和消息槽 | 非关键详情 | 不进入列表 | FIFO 顺序、游标范围和槽内容 |
| Messagequeue | receiver/sender waiter count | 关键 | 固定增加 | 完整 RX/TX 等待线程对象 |
| Messagequeue | receiver/sender names | 非关键弹性文本 | 保留，可截断 | 完整 RX/TX 名称列表 |
| Messagequeue | `Free` | 非关键单值 | 120 列预算内保留 | `max_msgs-entry` 一致性 |
| Messagequeue | `Policy` | 非关键单值 | 120 列预算内保留 | 原始 IPC flag |
| Messagequeue | `head/tail/free` 和消息节点 | 非关键详情 | 不进入列表 | payload 遍历和链表一致性 |
| Mempool | waiter count | 关键 | 固定增加到 `Waiters` | 完整等待线程对象 |
| Mempool | waiter names | 非关键弹性文本 | 保留，可截断 | 完整名称列表 |
| Mempool | `Used` | 非关键单值 | 120 列预算内保留 | total/free/used 一致性和使用率 |
| Mempool | pool 和 block-list 指针 | 非关键详情 | 不进入列表 | 范围、对齐和 free-list 检查 |

上述 120 列评估以 64 位地址的最坏固定宽度为基线，并允许对
`Name/Owner/Waiters/Callback/Entry` 设置合理的弹性宽度。数值、状态、数量
和地址列不允许为了适配宽度而截断。预算评估使用 `Name=12`、`Owner=12`、
每个 waiter cell `=18`、`Callback/Entry=20`、64 位 `Addr/SP=18` 和双空格
列间距；这些只是设计估值，运行时仍根据真实内容重新计算。

| 对象 | 计划紧凑列 | 64 位预算估值 | 120 字符结论 |
|---|---|---:|---|
| Thread | Name、State、Prio、BasePrio、SP、Stack、Used、HighWater、Entry、按能力 CPU/Bind、Addr | SMP 约 136 | 收缩 Name/Entry 后保留全部列；Error/Remain 进 detail |
| Timer | Name、State、Mode、Type、InitTick、TimeoutTick、ExpiresIn、Callback、Addr | 约 117 | 保留全部列 |
| Semaphore | Name、Value、Waiters、Policy、Addr | 约 67 | 保留全部列 |
| Mutex | Name、Value、Hold、Owner、OriginalPrio、Waiters、Policy、Addr | 约 101 | 保留全部列 |
| Event | Name、Set、Waiters、Policy、Addr | 约 72 | 保留全部列；waiter 条件进 detail |
| Mailbox | Name、Entry、Size、Free、In、Out、RecvWait、SendWait、Policy、Addr | 约 109 | 保留全部列 |
| Messagequeue | Name、Entry、MsgSize、MaxMsgs、Free、RecvWait、SendWait、Policy、Addr | 约 111 | 保留全部列 |
| Mempool | Name、BlockSize、Total、Free、Used、Waiters、Addr | 约 82 | 保留全部列 |

若真实数据超过上述估值，使用 Phase A 的运行时截断算法。不得为了追求严格
120 字符而删除已经确认保留的非关键单值列。

等待队列位置固定为：

| 对象 | 接收/资源等待队列 | 发送等待队列 |
|---|---|---|
| Semaphore、Mutex、Event | `parent.suspend_thread` | 不适用 |
| Mailbox | `parent.suspend_thread` | `suspend_sender_thread` |
| Messagequeue | `parent.suspend_thread` | 版本支持时为 `suspend_sender_thread` |
| Mempool | `suspend_thread` | 不适用 |

Event 的等待条件不在 event 对象本身，而在挂起线程的 `event_set` 和
`event_info` 中。只有把等待线程和这两个字段关联起来，才能解释线程等待的
mask、AND/OR/CLEAR 模式以及为何当前 `event.set` 没有唤醒它。

### 11.4 版本兼容边界

Messagequeue sender wait list：

- `v3.1.0-v3.1.3`：没有 `suspend_sender_thread`；
- `v3.1.4-v3.1.5`：存在 `suspend_sender_thread`；
- `v4.0.0-v4.0.1`：没有 `suspend_sender_thread`；
- `v4.0.2-v4.1.1`：存在 `suspend_sender_thread`。

Mempool suspend count：

- `v3.1.0-v3.1.3`、`v4.0.0-v4.0.1`：存在
  `suspend_thread_count`；
- `v3.1.4-v3.1.5`、`v4.0.2-v4.1.1`：该字段已删除；
- 全部目标版本均有 `suspend_thread` 链表，因此统一遍历链表计数。

当前 `build_messagequeue_layout()` 无版本参数且无条件描述 sender list。
实现等待者遍历时，应按上述版本边界构建字段，或基于目标 DWARF 字段存在性
明确标记该能力。当前 mempool layout 还缺少 `block_list` 和
`suspend_thread`，必须先补齐 `suspend_thread`；`block_list` 只为 detail
检查保留，不要求进入默认表。

### 11.5 Thread/SMP 已确认缺陷

当前 thread converter 使用：

```python
bind_cpu=read_int(...) or -1
oncpu=read_int(...) or -1
```

合法 CPU 编号 `0` 会被当成假值并错误转换为 `-1`。必须改成显式区分
`None` 和整数零。

当前 task summary 还把选中的 current thread 固定标记成 core 0，没有使用
线程的 `oncpu/bind_cpu`。SMP 输出应显示实际 CPU 归属；UP 目标仍可使用
core 0/current marker。实现时还要确认各版本“未绑定”和“当前未运行”的
sentinel 定义，不能把所有负值或最大无符号值直接当成有效 CPU。

`TaskSummary` 已有 `address/base_priority/current_core`，但通用 task renderer
尚未显示地址和基础优先级。增加这些公共列会影响 FreeRTOS 输出和对应测试，
必须同步验证两个 adapter；RT-Thread 特有的 `error/remaining_tick/bind_cpu`
若进入公共 API，应使用可选字段，避免把 RT-Thread 语义硬编码进 core。

### 11.6 实施 Phase A：宽度感知列表与对象 Detail

本 Phase 是新增打印字段的前置工作，只实现 GDR 实际需要的最小表格能力，
不引入 `prettytable` 等外部运行时依赖，也不复制 GEF 的平台相关表格代码。
保留现有 ASCII 表格、双空格分列和单次 `gdb.write()` 行为。

#### 11.6.1 终端宽度

每次渲染列表前检查当前终端宽度，优先级固定为：

1. `gdb.parameter("width")` 返回的正整数，即用户显式执行的
   `set width N`；
2. GDB width 为 unlimited/`None` 时，使用标准库
   `shutil.get_terminal_size(fallback=(120, 24)).columns`；
3. 无法取得有效正整数时回退为 120。

格式化核心必须接受显式 `width` 参数，终端探测与纯格式化逻辑分离，以便
单元测试稳定覆盖 80、100、120 和 160 字符。不得解析本地化的
`show width` 文本，也不需要照搬 GEF 的 Unix `ioctl`/Windows API 分支。

#### 11.6.2 稳定紧凑表和截断算法

`rtt <objects>` 的列集合由第 11.3 节固定，不根据终端宽度隐藏或新增列。
自然表格宽度计算为全部列宽之和，加相邻列之间的两个空格。

当自然宽度超过当前终端宽度时：

1. 只允许收缩 `Name/Owner/Waiters/Callback/Entry` 等显式标记的弹性文本列；
2. 收缩顺序为 `Waiters`、`Callback/Entry`、`Owner`、`Name`，同一优先级
   先收缩当前最宽的列；
3. 表头不截断。每个弹性列最小宽度为 `max(len(header), 5)`；
4. 单元格超过分配宽度时输出 `text[:width-2] + ".."`；因此最短文本是
   3 个原字符加 2 个点，禁止使用 Unicode 省略号；
5. `Waiters` cell 必须把数量放在最前面，例如 `2:worker,logger`，保证截断
   后仍优先保留关键 count；
6. 数字、枚举状态、地址和表头不截断，也不自动删除任何列；
7. 如果全部弹性列缩到最小后仍超过终端宽度，放弃本次全部截断结果，按
   原始自然列宽输出。允许终端自行换行，不实现二次布局、纵向回退或隐藏列。

120 字符只是设计紧凑列表字段集合时的基准，不是强制运行时宽度。80 字符
终端可能触发截断，低于最小可表示宽度时按第 7 条保持原格式。

#### 11.6.3 单对象 Detail 命令

保留现有复数列表命令，并新增下列单数形式：

```text
rtt thread <obj_name>
rtt timer <obj_name>
rtt semaphore <obj_name>
rtt mutex <obj_name>
rtt event <obj_name>
rtt mailbox <obj_name>
rtt messagequeue <obj_name>
rtt mempool <obj_name>
```

Detail 使用纵向 `Key: Value`，不受横向表格列宽限制。公共部分至少显示名称、
地址、对象类型和列表中的全部字段；对象特有部分可显示内部指针、完整等待
线程名称、Event 等待条件和一致性检查。对象不存在、对象类型未启用或字段在
当前版本不可用时必须输出明确诊断。

Detail 不替代 `$gdr_object()`：前者提供可读摘要和校验，后者继续返回原生
`gdb.Value` 供任意表达式访问。内部链表遍历仍必须有节点上限和损坏保护。

#### 11.6.4 Phase A TODO

- [x] 在 `gdr/gdb_bridge.py` 增加可单元测试的终端宽度探测函数；
- [x] 为 `print_table()` 增加显式 width 和弹性列元数据，保持旧调用兼容；
- [x] 实现确定性的弹性列收缩、两个点截断和“最小仍超宽则恢复自然表格”；
- [x] 保持空表输出和完整表格单次 `gdb.write()`；
- [x] 在 `ObjectTable` 中表达弹性列，不依赖 renderer 猜测表头文本；
- [x] 扩展 RT-Thread 命令解析，保留复数列表并支持单数 detail 语法；
- [x] 增加纵向 key/value detail renderer，避免 RT-Thread 字段进入 generic
  renderer；
- [x] 单元测试覆盖显式 GDB width、unlimited width、系统终端回退和 120
  默认值；
- [x] 单元测试覆盖无需截断、单列截断、多列按优先级截断、最小宽度、表头
  不截断、关键 count 保留和最小仍超宽时恢复原格式；
- [x] 集成测试固定 GDB width 后验证 80/120 列输出以及 detail 命令；
- [x] 文档说明列表输出可能被终端换行，但列集合不会随宽度变化。

### 11.7 TODO：P0 阻塞关系和准确性

- [x] 在 `rtthread/navigation.py` 增加有界的 suspend-list 遍历辅助函数，
  使用 `struct rt_thread.tlist` 恢复线程对象并返回稳定的名称列表；
- [x] 为等待列表增加坏指针、闭环异常和节点上限保护，错误时显示明确的
  `<invalid>`/truncated 信息；
- [x] 在 semaphore 表增加 `count:names` 形式的等待线程摘要；
- [x] 在 mutex 表增加 `count:names` 形式的等待线程摘要；
- [x] 在 event 表增加 `count:names` 形式的等待线程摘要，并在 event detail
  中显示每个 waiter 的 `event_set/event_info` 条件；
- [x] 在 mailbox 表分别增加 `count:names` 形式的 receiver 和 sender waiters；
- [x] 在 messagequeue 表分别增加 `count:names` 形式的 receiver 和 sender
  waiters，旧版本没有 sender list 时显示 `N/A`，不能显示伪造的 `0`；
- [x] 在 mempool layout 增加 `suspend_thread` 并显示 `count:names` 摘要；
- [x] 所有 waiter count 通过链表遍历得出，不读取旧版
  `suspend_thread_count`；
- [x] 为新增字段添加 layout、navigation、adapter 单元测试；版本边界由 QEMU
  集成测试在真实内核上验证（见 11.10）。

### 11.8 TODO：P1 默认表增强和缺陷修复

- [ ] Mutex 打印 converter 已读取的 `original_priority`，用于分析 priority
  inheritance 和 priority inversion；
- [ ] Mailbox 增加派生 `Free = size - entry`；
- [ ] Messagequeue 增加派生 `Free = max_msgs - entry`；
- [ ] Mempool 增加派生 `Used = total - free`，必要时再增加使用率；
- [ ] Timer 表增加 `Addr`；
- [ ] Timer 增加回绕安全的 `ExpiresIn`，inactive timer 显示 `N/A`；
- [ ] Task 表显示已有的 `Addr` 和 `BasePrio`，同步更新 FreeRTOS 单元与集成
  测试；
- [ ] 修复 `bind_cpu/oncpu` 将合法 CPU 0 转换为 `-1` 的问题；
- [ ] SMP task summary 使用真实 `oncpu`，并按目标能力显示 `CPU/Bind`；
- [ ] 在 120 列预算内增加 IPC `FIFO/PRIO` policy 列，确保 flag 解码覆盖
  全部目标版本；
- [ ] 为所有新增派生值覆盖空、满、inactive、tick 回绕和非法原始值边界。

### 11.9 TODO：P2 Detail 和高级诊断

- [ ] 为 Phase A 的 object detail 补齐各对象的特有字段和校验结果；
- [ ] Timer detail 显示 callback `parameter`；
- [ ] Messagequeue detail 从 `msg_queue_head` 有界遍历消息节点，并按目标版本
  的节点头尺寸定位 payload；
- [ ] Messagequeue detail 校验 `entry` 与活动链表节点数、free list 节点数及
  `max_msgs` 的一致性；
- [ ] Mailbox detail 根据 `out_offset/entry/size` 按 FIFO 顺序显示消息槽，
  并校验 offset 范围；
- [ ] Mempool detail 显示 `start_address/size/block_list`，校验池范围、块对齐
  和 free count；
- [ ] Thread detail 显示 `error/remaining_tick`，避免扩大通用任务表。

### 11.10 Fixture 和 QEMU 闭环 TODO

- [ ] 扩展 Cortex-A9 fixture，创建确定性阻塞线程：空 semaphore waiter、
  mutex owner/waiter、event mask waiter、mailbox receiver/sender、messagequeue
  receiver/sender、耗尽后的 mempool waiter；
- [ ] 若同一 mailbox/messagequeue 无法同时稳定表示空等待和满等待，创建
  独立的 RX/TX fixture 对象，不依赖测试执行过程中修改目标状态；
- [ ] 对不支持 MQ sender wait list 的旧版本只验证 receiver waiters 和
  `SendWaiters=N/A`；
- [ ] fixture ready marker 只能在对象创建、线程进入预期阻塞状态后输出；
- [ ] 测试期望继续存放于 `tests/support/rtthread_profiles.py` 和
  `tests/support/rtthread_qemu_profiles.py`，禁止从生产 layout 自动生成；
- [ ] 更新 `tests/integration/rtthread/test_commands.py`，逐行核对新增列、
  waiter 名称、event mask/mode 和派生容量；
- [ ] 更新 `tests/integration/rtthread/test_functions.py`，确认
  `$gdr_object()` 仍返回可继续访问底层字段的原始对象；
- [ ] 更新 adapter/layout/navigation 单元测试，覆盖全部字段边界；
- [ ] 使用 `ci/rt-thread/run-qemu-matrix.sh cortex-a9` 构建全部 tag，并在
  `v3.1.0/v3.1.3/v3.1.5/v4.0.0/v4.0.2/v4.0.5/v4.1.1` 执行完整 QEMU 测试；
- [ ] 使用 `ci/rt-thread/run-qemu-matrix.sh rv64` 验证 v4.0.4-v4.1.1 无回归；
- [ ] runner 不重新引入自定义 `OUT_ELF/OUT_BIN`，继续通过统一
  `BUILD_DIR` 下的 BSP 默认产物和 `GDR_ELF_PATH/GDR_FIRMWARE_PATH` 定位；
- [ ] 复用的 RT-Thread clone 每次构建前执行现有 `git reset --hard <ref>`
  和 `git clean -ffdx`，清除全部 SCons 输出和 ignored cache；
- [ ] patch 必须按实际 `main.c` 基线分组；公共 patch 无法跨版本干净应用时，
  从对应 tag 的已修改源码重新提取包含 `main()` 的版本专用 patch。

### 11.11 明确不进入默认表的字段

以下字段默认不打印，只通过 `$gdr_object()` 或 P2 detail 功能访问：

- messagequeue 的 `msg_queue_head/msg_queue_tail/msg_queue_free` 裸指针；
- mailbox 的 `msg_pool` 裸指针和全部未使用槽位；
- mempool 的 `start_address/size/block_list` 内部指针；
- 通用 intrusive list 节点地址；
- semaphore `reserved`；
- timer callback `parameter`；
- event waiter 的 `event_set/event_info` 条件信息。

### 11.12 验收标准

1. 所有 IPC/mempool 表均可显示接收或资源等待线程，mailbox/MQ 在版本支持
   时可单独显示发送等待线程；
2. Event detail 可以从当前 set 和 waiter 的 mask/mode 解释其未唤醒原因；
3. Mutex 输出包含 owner、hold、原始优先级和等待者，可用于分析优先级继承；
4. Messagequeue 不出现 `in_offset/out_offset`，mailbox 继续准确打印这两个
   环形游标；
5. Mempool waiter count 在有无 `suspend_thread_count` 的版本上结果一致；
6. MQ sender list 缺失的旧版本显示 `N/A` 且不会触发 GDB field access error；
7. SMP CPU 0 不再被显示为 `-1`，UP 输出无回归；
8. 所有链表遍历面对损坏内存都能有界退出并给出诊断；
9. 单元测试、ruff 和 format check 全部通过；
10. Cortex-A9 全版本可构建，代表版本 QEMU 闭环全部通过，RV64 v4.0.4-v4.1.1
    无回归；
11. CI runner 保持统一 `BUILD_DIR` 工作目录，不依赖自定义
    `OUT_ELF/OUT_BIN`；
12. `rtt <objects>` 的列集合不随终端宽度变化，80/120/160 字符下的截断
    行为符合 Phase A 定义；
13. 弹性列最短显示 3 个原字符和 2 个点；最小仍超宽时恢复自然表格输出，
    不隐藏列或切换其他布局；
14. 所有支持对象均可通过 `rtt <object> <obj_name>` 查看纵向 detail，且
    不影响 `$gdr_object()` 返回原生值；
15. 中英文 README 和架构文档同步新增列、版本限制、waiter 语义、宽度处理
    和 detail 能力边界。
