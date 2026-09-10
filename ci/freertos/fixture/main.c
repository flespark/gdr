/* Shared GDR FreeRTOS QEMU fixture.
 *
 * Variant differences are selected by FreeRTOSConfig.h knobs
 * (configSUPPORT_*_ALLOCATION, configUSE_QUEUE_SETS, configUSE_TRACE_FACILITY,
 * tskKERNEL_VERSION_*). Heap_5 region setup is gated by GDR_FIXTURE_HEAP_5.
 *
 * The kernel/config/board headers below resolve only through the ARM cross
 * include paths supplied by ci/freertos/build-fixture-kernel.sh; host-side
 * analyzers without those paths report spurious "file not found" errors
 * (see .pi-lens.json ignore for the same class).
 */
#include "FreeRTOS.h"
#include "event_groups.h"
#include "queue.h"
#include "semphr.h"
#include "stream_buffer.h"
#include "task.h"
#include "timers.h"

/* Reason: kernel 10.3.1's FreeRTOS.h predates the SMP configNUMBER_OF_CORES
 * knob (added in V11); V11 headers default it to 1 themselves.  Provide the
 * same default here so the shared fixture's SMP-gated expressions compile on
 * every supported kernel generation. */
#ifndef configNUMBER_OF_CORES
#define configNUMBER_OF_CORES 1
#endif

#if (tskKERNEL_VERSION_MAJOR > 11) || \
    (tskKERNEL_VERSION_MAJOR == 11 && tskKERNEL_VERSION_MINOR >= 1)
#include "message_buffer.h"
#define GDR_HAS_BATCHING_BUFFER 1
#else
#include "message_buffer.h"
#define GDR_HAS_BATCHING_BUFFER 0
#endif

#define GDR_USED __attribute__((used))

#ifndef GDR_FIXTURE_MIXED_ALLOCATION
#define GDR_FIXTURE_MIXED_ALLOCATION 0
#endif

/* Packed decimal encoding of the kernel version for CU-independent probing.
 * The .rodata.gdr_version section name trips host-side clang's
 * attribute_section_invalid_for_target check, but the section is a plain
 * string literal that the ARM cross toolchain accepts -- a false positive. */
GDR_USED __attribute__((section(".rodata.gdr_version")))
const uint32_t gdr_freertos_version_num =
    (uint32_t)tskKERNEL_VERSION_MAJOR * 10000U +
    (uint32_t)tskKERNEL_VERSION_MINOR * 100U +
    (uint32_t)tskKERNEL_VERSION_BUILD;

#ifndef GDR_PORT_PROVIDES_SYSTICK_HANDLER
extern void xPortSysTickHandler(void);
#endif
#if (configUSE_TICK_HOOK == 1)
extern void gdr_runtime_timer_tick(void);
#endif

#ifndef configMINIMAL_STACK_SIZE
#define configMINIMAL_STACK_SIZE ((uint16_t)128)
#endif

#define GDR_STACK_WORDS ((uint32_t)configMINIMAL_STACK_SIZE)
#define GDR_EXHAUST_STACK_WORDS ((uint32_t)(configMINIMAL_STACK_SIZE * 2U))

#if (configSUPPORT_STATIC_ALLOCATION == 1)
static StaticTask_t gdr_idle_tcb;
static StackType_t gdr_idle_stack[GDR_STACK_WORDS];
#if (configUSE_TIMERS == 1)
static StaticTask_t gdr_timer_tcb;
static StackType_t gdr_timer_stack[configTIMER_TASK_STACK_DEPTH];
#endif
#endif

GDR_USED static TaskHandle_t gdr_high_task;
GDR_USED static TaskHandle_t gdr_normal_task;
GDR_USED static TaskHandle_t gdr_low_task;
GDR_USED static TaskHandle_t gdr_queue_recv_task;
GDR_USED static TaskHandle_t gdr_queue_send_task;
GDR_USED static TaskHandle_t gdr_sem_take_task;
GDR_USED static TaskHandle_t gdr_mutex_take_task;
GDR_USED static TaskHandle_t gdr_mutex_hold_task;
GDR_USED static TaskHandle_t gdr_event_wait_task;
GDR_USED static TaskHandle_t gdr_notify_wait_task;
GDR_USED static TaskHandle_t gdr_maxdelay_task;
GDR_USED static TaskHandle_t gdr_suspended_task;
GDR_USED static TaskHandle_t gdr_recursive_task;
GDR_USED static TaskHandle_t gdr_exhaust_task;
GDR_USED static TaskHandle_t gdr_ready_spin_task;
GDR_USED static TaskHandle_t gdr_waiter_only_eg_handle;
#if (configNUMBER_OF_CORES > 1)
GDR_USED static TaskHandle_t gdr_bound_task;
GDR_USED static TaskHandle_t gdr_preempt_task;
#endif

GDR_USED static QueueHandle_t gdr_registered_queue;
GDR_USED static QueueHandle_t gdr_unregistered_queue;
GDR_USED static QueueHandle_t gdr_empty_queue;
GDR_USED static QueueHandle_t gdr_full_queue;
GDR_USED static QueueHandle_t gdr_maxdelay_queue;
GDR_USED static SemaphoreHandle_t gdr_semaphore;
GDR_USED static SemaphoreHandle_t gdr_mutex;
GDR_USED static SemaphoreHandle_t gdr_recursive_mutex;
GDR_USED static TimerHandle_t gdr_active_timer;
GDR_USED static TimerHandle_t gdr_inactive_timer;
GDR_USED static TimerHandle_t gdr_stopped_timer;
GDR_USED static TimerHandle_t gdr_oneshot_timer;
GDR_USED static TimerHandle_t gdr_created_idle_timer;
GDR_USED static EventGroupHandle_t gdr_event_group;
GDR_USED static EventGroupHandle_t gdr_static_event_group;
GDR_USED static StreamBufferHandle_t gdr_stream_buffer;
GDR_USED static StreamBufferHandle_t gdr_message_buffer;
#if (GDR_HAS_BATCHING_BUFFER == 1)
GDR_USED static StreamBufferHandle_t gdr_batching_buffer;
#endif
#if (GDR_FIXTURE_STREAM_ACTIONS == 1)
/* Reason: the deleted-buffer detail (Type: deleted) needs a handle symbol
 * that survives vStreamBufferDelete; a dynamic buffer would be freed and
 * the memory reused, so the deleted one is static. */
GDR_USED static StreamBufferHandle_t gdr_deleted_stream_buffer;
#endif
#if (configUSE_QUEUE_SETS == 1)
GDR_USED static QueueSetHandle_t gdr_queue_set;
GDR_USED static QueueHandle_t gdr_set_member_a;
GDR_USED static QueueHandle_t gdr_set_member_b;
#endif

#if (configSUPPORT_STATIC_ALLOCATION == 1) && \
    (GDR_FIXTURE_MIXED_ALLOCATION == 0)
static StaticTask_t gdr_ready_tcb;
static StackType_t gdr_ready_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_normal_tcb;
static StackType_t gdr_normal_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_low_tcb;
static StackType_t gdr_low_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_queue_recv_tcb;
static StackType_t gdr_queue_recv_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_queue_send_tcb;
static StackType_t gdr_queue_send_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_sem_take_tcb;
static StackType_t gdr_sem_take_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_mutex_take_tcb;
static StackType_t gdr_mutex_take_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_mutex_hold_tcb;
static StackType_t gdr_mutex_hold_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_event_wait_tcb;
static StackType_t gdr_event_wait_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_notify_wait_tcb;
static StackType_t gdr_notify_wait_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_maxdelay_tcb;
static StackType_t gdr_maxdelay_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_suspended_tcb;
static StackType_t gdr_suspended_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_recursive_tcb;
static StackType_t gdr_recursive_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_exhaust_tcb;
static StackType_t gdr_exhaust_stack_mem[GDR_EXHAUST_STACK_WORDS];
static StaticTask_t gdr_ready_spin_tcb;
static StackType_t gdr_ready_spin_stack[GDR_STACK_WORDS];
static StaticTask_t gdr_waiter_only_eg_tcb;
static StackType_t gdr_waiter_only_eg_stack[GDR_STACK_WORDS];

static StaticQueue_t gdr_registered_queue_buf;
static uint8_t gdr_registered_queue_storage[4 * sizeof(uint32_t)];
static StaticQueue_t gdr_unregistered_queue_buf;
static uint8_t gdr_unregistered_queue_storage[2 * sizeof(uint32_t)];
static StaticQueue_t gdr_empty_queue_buf;
static uint8_t gdr_empty_queue_storage[2 * sizeof(uint32_t)];
static StaticQueue_t gdr_full_queue_buf;
static uint8_t gdr_full_queue_storage[1 * sizeof(uint32_t)];
static StaticQueue_t gdr_maxdelay_queue_buf;
static uint8_t gdr_maxdelay_queue_storage[1 * sizeof(uint32_t)];
static StaticQueue_t gdr_semaphore_buf;
static StaticQueue_t gdr_mutex_buf;
static StaticQueue_t gdr_recursive_mutex_buf;
static StaticTimer_t gdr_active_timer_buf;
static StaticTimer_t gdr_inactive_timer_buf;
static StaticTimer_t gdr_stopped_timer_buf;
static StaticTimer_t gdr_oneshot_timer_buf;
static StaticTimer_t gdr_created_idle_timer_buf;
static StaticEventGroup_t gdr_event_group_buf;
static StaticEventGroup_t gdr_static_event_group_buf;
static StaticStreamBuffer_t gdr_stream_buffer_struct;
static uint8_t gdr_stream_buffer_storage[32];
static StaticStreamBuffer_t gdr_message_buffer_struct;
static uint8_t gdr_message_buffer_storage[32];
#if (configUSE_QUEUE_SETS == 1)
static StaticQueue_t gdr_queue_set_buf;
static uint8_t gdr_queue_set_storage[2 * sizeof(void *)];
static StaticQueue_t gdr_set_member_a_buf;
static uint8_t gdr_set_member_a_storage[1 * sizeof(uint32_t)];
static StaticQueue_t gdr_set_member_b_buf;
static uint8_t gdr_set_member_b_storage[1 * sizeof(uint32_t)];
#endif
#endif /* static-only object storage */

#if (configSUPPORT_STATIC_ALLOCATION == 1) && \
    (GDR_FIXTURE_MIXED_ALLOCATION == 1)
/* Mixed allocation deliberately keeps one task and one queue static. */
static StaticTask_t gdr_ready_tcb;
static StackType_t gdr_ready_stack[GDR_STACK_WORDS];
static StaticQueue_t gdr_registered_queue_buf;
static uint8_t gdr_registered_queue_storage[4 * sizeof(uint32_t)];
/* Reason: the static-dynamic variant must own a genuinely static event group
 * (ucStaticallyAllocated == 1) while keeping another EG dynamic; the static
 * storage block below is the only place a buffer for it can live. */
static StaticEventGroup_t gdr_static_event_group_buf;
#if (GDR_FIXTURE_STREAM_ACTIONS == 1)
static StaticStreamBuffer_t gdr_deleted_stream_buffer_buf;
static uint8_t gdr_deleted_stream_buffer_storage[8];
#endif
#endif

#ifndef GDR_BOARD_PROVIDES_SEMIHOSTING
void gdr_semihosting_write0(const char *message)
{
    register int operation __asm__("r0") = 0x04;
    register const char *argument __asm__("r1") = message;
    __asm__ volatile("bkpt 0xab" : : "r"(operation), "r"(argument) : "memory");
}
#else
/* Reason: the RISC-V board provides its own ebreak-sequenced
 * gdr_semihosting_write0 (qemu-virt-rv64/system_init.c); keep a prototype
 * so the ready-marker call site compiles without an implicit declaration. */
void gdr_semihosting_write0(const char *message);
#endif

void gdr_fixture_assert_failed(int line)
{
    (void)line;
    taskDISABLE_INTERRUPTS();
    for (;;) {
    }
}

void vApplicationMallocFailedHook(void)
{
    GDR_FIXTURE_UNREACHABLE();
}

#if (configENABLE_HEAP_PROTECTOR == 1)
/* Reason: V11.0.0+ heap_4.c externs vApplicationGetRandomHeapCanary and calls
 * it from prvHeapInit; the kernel ships no default, so without this the
 * heap-protector variant fails to link on the kernel-direct lane. The canary
 * is a fixed non-zero constant, not true randomness, so the GDB side can
 * XOR it back and cross-check that decoded heap pointers were really
 * heapPROTECT_BLOCK_POINTER-obfuscated (a zero canary would silently degrade
 * to the unprotected layout and prove nothing). */
void vApplicationGetRandomHeapCanary(portPOINTER_SIZE_TYPE *pxHeapCanary)
{
    *pxHeapCanary = (portPOINTER_SIZE_TYPE)0xBEEFU;
}
#endif

#if (configCHECK_FOR_STACK_OVERFLOW > 0)
void vApplicationStackOverflowHook(TaskHandle_t task, char *name)
{
    (void)task;
    (void)name;
    GDR_FIXTURE_UNREACHABLE();
}
#endif

#if (configSUPPORT_STATIC_ALLOCATION == 1)
/* Reason: the idle/timer static-memory callbacks' third parameter must match
 * the linked kernel's declaration byte-for-byte. Kernels < 11.0.0 declare it
 * as uint32_t * via an extern local to tasks.c/timers.c (NOT in task.h) and
 * pass an *uninitialised* local; 10.3.1 additionally defaults
 * configSTACK_DEPTH_TYPE to uint16_t, so writing through a uint16_t * only
 * fills the low half and prvInitialiseNewTask's stack memset runs away with
 * stack garbage in the upper half. V11.0.0+ declare the parameter as
 * configSTACK_DEPTH_TYPE * inside task.h, so the width must follow the kernel
 * major version, not configSTACK_DEPTH_TYPE's own existence.
 * SMP note: with configNUMBER_OF_CORES > 1 on V11, the kernel also calls
 * vApplicationGetPassiveIdleTaskMemory(..., configSTACK_DEPTH_TYPE *,
 * BaseType_t xCoreID) per extra core; not needed by this single-core
 * fixture. */
#if (tskKERNEL_VERSION_MAJOR >= 11)
typedef configSTACK_DEPTH_TYPE gdr_stack_depth_t;
#else
typedef uint32_t gdr_stack_depth_t;
#endif

void vApplicationGetIdleTaskMemory(StaticTask_t **tcb_buffer,
                                   StackType_t **stack_buffer,
                                   gdr_stack_depth_t *stack_size)
{
    *tcb_buffer = &gdr_idle_tcb;
    *stack_buffer = gdr_idle_stack;
    *stack_size = (gdr_stack_depth_t)GDR_STACK_WORDS;
}

#if (configUSE_TIMERS == 1)
void vApplicationGetTimerTaskMemory(StaticTask_t **tcb_buffer,
                                    StackType_t **stack_buffer,
                                    gdr_stack_depth_t *stack_size)
{
    *tcb_buffer = &gdr_timer_tcb;
    *stack_buffer = gdr_timer_stack;
    *stack_size = (gdr_stack_depth_t)configTIMER_TASK_STACK_DEPTH;
}
#endif
#endif

#if (INCLUDE_xTimerPendFunctionCall == 1)
/* Reason: the pended callback's address and arguments must be deterministic
 * for the command-table assertions (Value column decodes them); the values
 * are arbitrary but stable. */
static void gdr_pended_function(void *pvParameter1, uint32_t ulParameter2)
{
    (void)pvParameter1;
    (void)ulParameter2;
}
#endif

static void gdr_timer_callback(TimerHandle_t timer)
{
    (void)timer;
}

static void gdr_delay_task(void *argument)
{
    (void)argument;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/* Lowest-priority spinner so the State column has a genuine Ready row
 * besides IDLE (which is Running). Yields so IDLE can still run. */
static void gdr_ready_spin(void *argument)
{
    (void)argument;
    for (;;) {
        taskYIELD();
    }
}

static void gdr_queue_recv(void *argument)
{
    uint32_t value;
    (void)argument;
    for (;;) {
        (void)xQueueReceive(gdr_empty_queue, &value, portMAX_DELAY);
    }
}

static void gdr_queue_send(void *argument)
{
    uint32_t value = 1U;
    (void)argument;
    for (;;) {
        (void)xQueueSend(gdr_full_queue, &value, portMAX_DELAY);
    }
}

static void gdr_sem_take(void *argument)
{
    (void)argument;
    for (;;) {
        (void)xSemaphoreTake(gdr_semaphore, portMAX_DELAY);
    }
}

/* Reason: the holder must be a real task, not main().  A take issued before
 * vTaskStartScheduler() records pxCurrentTCB == NULL as the holder, which
 * leaves Owner/OwnerPriority unobservable and makes the mutex accounting
 * invariant (count + holder != NULL == 1) permanently fail.  Priority 1 is
 * below the waiter's 3 so the take also exercises priority inheritance:
 * uxPriority becomes 3 while uxBasePriority stays 1. */
static void gdr_mutex_hold(void *argument)
{
    (void)argument;
    configASSERT(xSemaphoreTake(gdr_mutex, portMAX_DELAY) == pdPASS);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void gdr_mutex_take(void *argument)
{
    (void)argument;
    /* Reason: yield long enough for the lower-priority holder to acquire the
     * mutex first; otherwise this higher-priority task would take it itself
     * and no contended-mutex state would exist. The ready marker fires at
     * 80 ms, well after this window. */
    vTaskDelay(pdMS_TO_TICKS(20));
    for (;;) {
        (void)xSemaphoreTake(gdr_mutex, portMAX_DELAY);
    }
}

static void gdr_event_wait(void *argument)
{
    (void)argument;
    for (;;) {
        (void)xEventGroupWaitBits(gdr_event_group, 0x3U, pdFALSE, pdTRUE,
                                  portMAX_DELAY);
    }
}

static void gdr_notify_wait(void *argument)
{
    (void)argument;
    for (;;) {
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    }
}

static void gdr_maxdelay_recv(void *argument)
{
    uint32_t value;
    (void)argument;
    for (;;) {
        (void)xQueueReceive(gdr_maxdelay_queue, &value, portMAX_DELAY);
    }
}

static void gdr_suspended(void *argument)
{
    (void)argument;
    vTaskSuspend(NULL);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void gdr_recursive_hold(void *argument)
{
    (void)argument;
    configASSERT(xSemaphoreTakeRecursive(gdr_recursive_mutex, 0) == pdPASS);
    configASSERT(xSemaphoreTakeRecursive(gdr_recursive_mutex, 0) == pdPASS);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

/* Touch nearly the whole stack so HighWater is distinguishable from a
 * never-filled (no 0xa5) window. Keep a live frame so the task stays
 * Blocked rather than overflowing. */
static void gdr_exhaust_stack_task(void *argument)
{
    volatile uint8_t pad[GDR_EXHAUST_STACK_WORDS];
    (void)argument;
    for (uint32_t i = 0; i < sizeof(pad); i++) {
        pad[i] = (uint8_t)(i & 0xffU);
    }
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        (void)pad[0];
    }
}

/* Reason: the only live evidence of the heuristic waiter channel.  The EG's
 * handle (or buffer on static-only builds) lives purely on this task's stack
 * -- no global, no registry -- so the symbol/registry channels can never find
 * it; only the waiter channel's container_of reverse discovery (navigation.py
 * iter_waiter_hosts) reconstructs it from the creator's blocked xEventListItem.
 * gdr_evw's gdr_event_group has a global handle and cannot serve that proof. */
static void gdr_waiter_only_eg_task(void *argument)
{
    (void)argument;
#if (configSUPPORT_DYNAMIC_ALLOCATION == 1)
    EventGroupHandle_t eg = xEventGroupCreate();
    configASSERT(eg != NULL);
#else
    StaticEventGroup_t eg_buf;
    EventGroupHandle_t eg = xEventGroupCreateStatic(&eg_buf);
    configASSERT(eg != NULL);
#endif
    for (;;) {
        (void)xEventGroupWaitBits(eg, 0x5U, pdFALSE, pdTRUE, portMAX_DELAY);
    }
}

/* Reason: the SMP lane needs a task pinned to one core and one that runs
 * with preemption disabled, so the Affinity and PreemptionDisable cells
 * carry genuine fixture state.  Both are config-gated: single-core lanes
 * must keep their current object graph. */
#if (configNUMBER_OF_CORES > 1)
static void gdr_bound(void *argument)
{
    (void)argument;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}

static void gdr_preempt(void *argument)
{
    (void)argument;
    vTaskPreemptionDisable(NULL);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(500));
    }
}
#endif

/* Reason: the daemon command queue is normally drained within one daemon
 * pass, so a live fixture would only ever show an empty queue.  Suspending
 * the daemon (it is a task like any other) before enqueueing freezes the
 * queue contents: one pended callback (xMessageID < 0) plus the timer
 * commands below stay pending until the harness breakpoint.  The daemon's
 * pre-scheduler drain reads exactly the four main() commands (capacity 8),
 * so pcReadFrom lands at slot 4 and the seven pending messages wrap the
 * ring during the read -- the wrap arm of iter_timer_commands gets its
 * first live evidence. */
#if (INCLUDE_xTimerPendFunctionCall == 1)
static void gdr_timer_command_task(void *argument)
{
    (void)argument;
    /* Let the daemon perform its first pass (it runs at configTIMER_TASK_
     * PRIORITY 3, above this task's 2) so xTimerQueue exists and the four
     * main() start/stop commands are drained deterministically. */
    vTaskDelay(pdMS_TO_TICKS(5));
    vTaskSuspend(xTimerGetTimerDaemonTaskHandle());
    configASSERT(xTimerPendFunctionCall(gdr_pended_function, (void *)0xCAFEF00DU,
                                        0x5A5A5A5AU, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_active_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_inactive_timer, 0) == pdPASS);
    configASSERT(xTimerChangePeriod(gdr_stopped_timer, pdMS_TO_TICKS(200), 0) ==
                 pdPASS);
    configASSERT(xTimerStart(gdr_oneshot_timer, 0) == pdPASS);
    configASSERT(xTimerChangePeriod(gdr_active_timer, pdMS_TO_TICKS(150), 0) ==
                 pdPASS);
    configASSERT(xTimerStart(gdr_created_idle_timer, 0) == pdPASS);
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
#endif

/* Reason: the ready marker used to fire after a 20 ms delay, before every
 * extra waiter had entered its stable blocked/suspended state. Stretch the
 * delay so QEMU's boot wait sees a deterministic object graph. */
static void gdr_ready_task(void *argument)
{
    (void)argument;
    /* Reason: on SMP lanes the created tasks first run in parallel across
     * the cores, so the mutex-holding / blocked / waiter states only reach
     * their stationary form after the whole task pack has had its first
     * run (measured: the object graph is stable from ~2 s on; a 2.5 s
     * marker lands the harness safely past that).  Single-core keeps the
     * historical 80 ms window where one core serialises the priority
     * order deterministically. */
#if (configNUMBER_OF_CORES > 1)
    vTaskDelay(pdMS_TO_TICKS(2500));
#else
    vTaskDelay(pdMS_TO_TICKS(80));
#endif
    gdr_semihosting_write0("GDR FreeRTOS fixture ready.\n");
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

#ifndef GDR_PORT_PROVIDES_SYSTICK_HANDLER
void SysTick_Handler(void)
{
    xPortSysTickHandler();
}
#else
/* Reason: the ARM_CM33_NTZ port defines SysTick_Handler itself in port.c
 * (no xPortSysTickHandler exists there), so this shared wrapper must not be
 * compiled on that board or the fixture gets a duplicate-symbol link error.
 * The board header declares GDR_PORT_PROVIDES_SYSTICK_HANDLER. */
#endif

#if (configUSE_TICK_HOOK == 1)
void vApplicationTickHook(void)
{
    gdr_runtime_timer_tick();
}
#endif

#if (configSUPPORT_DYNAMIC_ALLOCATION == 1)
/* Reason: MPU_xTaskCreate (v2 wrappers) refuses priorities without
 * portPRIVILEGE_BIT -- "xTaskCreate() can only be used to create
 * privileged tasks in MPU port"; the kernel masks the bit back off
 * (tasks.c uxPriority &= ~portPRIVILEGE_BIT), and the macro only exists on
 * MPU ports (ARM_CM3/CM4F have no such macro and no bit to add). */
#if defined(portPRIVILEGE_BIT)
#define GDR_TASK_PRIO(prio) ((prio) | portPRIVILEGE_BIT)
#else
#define GDR_TASK_PRIO(prio) (prio)
#endif

static TaskHandle_t gdr_create_task(TaskFunction_t fn, const char *name,
                                    uint16_t stack, UBaseType_t prio,
                                    TaskHandle_t *out)
{
    configASSERT(xTaskCreate(fn, name, stack, NULL, GDR_TASK_PRIO(prio),
                             out) == pdPASS);
    return *out;
}
#endif

#if (configSUPPORT_STATIC_ALLOCATION == 1)
static TaskHandle_t gdr_create_task_static(TaskFunction_t fn, const char *name,
                                           StackType_t *stack, uint32_t words,
                                           UBaseType_t prio, StaticTask_t *tcb,
                                           TaskHandle_t *out)
{
    *out = xTaskCreateStatic(fn, name, words, NULL, prio, stack, tcb);
    configASSERT(*out != NULL);
    return *out;
}
#endif

#if (configENABLE_MPU == 1)
/* Reason: the MPU wrapper's send path calls
 * xPortIsAuthorizedToAccessBuffer, which reads the *current* task's MPU
 * settings (xTaskGetMPUSettings asserts on NULL); before the scheduler
 * starts pxCurrentTCB is NULL, so every pre-scheduler xQueueSend /
 * xTimerStart faults.  Run the one-time priming from the highest-priority
 * task instead -- still deterministic and long before the ready marker. */
static void gdr_mpu_boot_priming(void *argument)
{
    (void)argument;
    uint32_t full_item = 1U;
    configASSERT(xQueueSend(gdr_full_queue, &full_item, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_active_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_stopped_timer, 0) == pdPASS);
    configASSERT(xTimerStop(gdr_stopped_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_oneshot_timer, 0) == pdPASS);
    vTaskDelete(NULL);
}
#endif

static void gdr_register(QueueHandle_t handle, const char *name)
{
#if (configQUEUE_REGISTRY_SIZE > 0)
    vQueueAddToRegistry(handle, name);
#else
    (void)handle;
    (void)name;
#endif
}

#ifdef GDR_FIXTURE_HEAP_5
/* Reason: heap_5 asserts that regions arrive with strictly increasing start
 * addresses (heap_5.c vPortDefineHeapRegions). Two separate globals have no
 * guaranteed link order -- a fixture build once placed the second array
 * below the first and died in that configASSERT before reaching
 * vTaskStartScheduler -- so the regions come from one ordered object.  The
 * heap-5-protector cell uses exactly one region so the protector's region
 * extremes (pucHeapLowAddress/HighAddress) span no gaps. */
static struct {
    uint8_t low[32 * 1024];
#if (GDR_FIXTURE_HEAP_5_SINGLE_REGION != 1)
    uint8_t high[16 * 1024];
#endif
} gdr_heap_arena __attribute__((aligned(8)));

static void gdr_init_heap_regions(void)
{
#if (GDR_FIXTURE_HEAP_5_SINGLE_REGION == 1)
    /* Reason: the heap-5-protector cell may only claim TotalSize from the
     * region extremes when they span exactly one region; multiple regions
     * would make pucHeapLowAddress/HighAddress span gaps.  One 32 KiB
     * region plus the 0-size terminator (heap_5 walks until
     * xSizeInBytes == 0). */
    const HeapRegion_t regions[] = {
        {gdr_heap_arena.low, sizeof(gdr_heap_arena.low)},
        {NULL, 0},
    };
#else
    /* Two live regions plus a trailing 0-size terminator (heap_5 walks
     * until xSizeInBytes == 0). The zero-size slot terminates the region list. */
    const HeapRegion_t regions[] = {
        {gdr_heap_arena.low, sizeof(gdr_heap_arena.low)},
        {gdr_heap_arena.high, sizeof(gdr_heap_arena.high)},
        {NULL, 0},
    };
#endif
    vPortDefineHeapRegions(regions);
}
#endif

int main(void)
{
#ifdef GDR_FIXTURE_HEAP_5
    gdr_init_heap_regions();
#endif

#if (configSUPPORT_DYNAMIC_ALLOCATION == 1)
#if (GDR_FIXTURE_MIXED_ALLOCATION == 1)
    gdr_registered_queue = xQueueCreateStatic(
        4, sizeof(uint32_t), gdr_registered_queue_storage,
        &gdr_registered_queue_buf);
#else
    gdr_registered_queue = xQueueCreate(4, sizeof(uint32_t));
#endif
    gdr_unregistered_queue = xQueueCreate(2, sizeof(uint32_t));
    gdr_empty_queue = xQueueCreate(2, sizeof(uint32_t));
    gdr_full_queue = xQueueCreate(1, sizeof(uint32_t));
    gdr_maxdelay_queue = xQueueCreate(1, sizeof(uint32_t));
    gdr_semaphore = xSemaphoreCreateCounting(3, 0);
    gdr_mutex = xSemaphoreCreateMutex();
    gdr_recursive_mutex = xSemaphoreCreateRecursiveMutex();
    gdr_active_timer = xTimerCreate(
        "gdr_active",
        /* Reason: on SMP lanes a short auto-reload wakes the timer daemon
         * every few ms, so a halted snapshot often catches it mid-command
         * and the active-list state reads racy (the detail command then
         * prints the mid-update warning).  A long period keeps the timer on
         * the active list while the daemon sleeps after boot, so the
         * assertions see stationary state. */
        pdMS_TO_TICKS(configNUMBER_OF_CORES > 1 ? 10000 : 100), pdTRUE, NULL,
        gdr_timer_callback);
    gdr_inactive_timer = xTimerCreate("gdr_idle", pdMS_TO_TICKS(100), pdFALSE,
                                      NULL, gdr_timer_callback);
    gdr_stopped_timer = xTimerCreate("gdr_stopped", pdMS_TO_TICKS(50), pdTRUE,
                                     NULL, gdr_timer_callback);
    gdr_oneshot_timer = xTimerCreate("gdr_oneshot", pdMS_TO_TICKS(1), pdFALSE,
                                     NULL, gdr_timer_callback);
    gdr_created_idle_timer = xTimerCreate("gdr_created", pdMS_TO_TICKS(100),
                                          pdFALSE, NULL, gdr_timer_callback);
    gdr_event_group = xEventGroupCreate();
    gdr_stream_buffer = xStreamBufferCreate(32, 1);
    gdr_message_buffer = xMessageBufferCreate(32);
#if (GDR_HAS_BATCHING_BUFFER == 1)
    gdr_batching_buffer = xStreamBatchingBufferCreate(32, 1);
#endif
#if (configUSE_QUEUE_SETS == 1)
    gdr_queue_set = xQueueCreateSet(2);
    gdr_set_member_a = xQueueCreate(1, sizeof(uint32_t));
    gdr_set_member_b = xQueueCreate(1, sizeof(uint32_t));
#endif
#else
    gdr_registered_queue = xQueueCreateStatic(
        4, sizeof(uint32_t), gdr_registered_queue_storage,
        &gdr_registered_queue_buf);
    gdr_unregistered_queue = xQueueCreateStatic(
        2, sizeof(uint32_t), gdr_unregistered_queue_storage,
        &gdr_unregistered_queue_buf);
    gdr_empty_queue = xQueueCreateStatic(2, sizeof(uint32_t),
                                         gdr_empty_queue_storage,
                                         &gdr_empty_queue_buf);
    gdr_full_queue = xQueueCreateStatic(1, sizeof(uint32_t),
                                        gdr_full_queue_storage,
                                        &gdr_full_queue_buf);
    gdr_maxdelay_queue = xQueueCreateStatic(1, sizeof(uint32_t),
                                            gdr_maxdelay_queue_storage,
                                            &gdr_maxdelay_queue_buf);
    gdr_semaphore = xSemaphoreCreateCountingStatic(3, 0, &gdr_semaphore_buf);
    gdr_mutex = xSemaphoreCreateMutexStatic(&gdr_mutex_buf);
    gdr_recursive_mutex =
        xSemaphoreCreateRecursiveMutexStatic(&gdr_recursive_mutex_buf);
    gdr_active_timer = xTimerCreateStatic("gdr_active", pdMS_TO_TICKS(100),
                                          pdTRUE, NULL, gdr_timer_callback,
                                          &gdr_active_timer_buf);
    gdr_inactive_timer = xTimerCreateStatic("gdr_idle", pdMS_TO_TICKS(100),
                                            pdFALSE, NULL, gdr_timer_callback,
                                            &gdr_inactive_timer_buf);
    gdr_stopped_timer = xTimerCreateStatic("gdr_stopped", pdMS_TO_TICKS(50),
                                           pdTRUE, NULL, gdr_timer_callback,
                                           &gdr_stopped_timer_buf);
    gdr_oneshot_timer = xTimerCreateStatic("gdr_oneshot", pdMS_TO_TICKS(1),
                                           pdFALSE, NULL, gdr_timer_callback,
                                           &gdr_oneshot_timer_buf);
    gdr_created_idle_timer = xTimerCreateStatic(
        "gdr_created", pdMS_TO_TICKS(100), pdFALSE, NULL, gdr_timer_callback,
        &gdr_created_idle_timer_buf);
    gdr_event_group = xEventGroupCreateStatic(&gdr_event_group_buf);
    gdr_stream_buffer = xStreamBufferCreateStatic(
        sizeof(gdr_stream_buffer_storage), 1, gdr_stream_buffer_storage,
        &gdr_stream_buffer_struct);
    gdr_message_buffer = xMessageBufferCreateStatic(
        sizeof(gdr_message_buffer_storage), gdr_message_buffer_storage,
        &gdr_message_buffer_struct);
#if (configUSE_QUEUE_SETS == 1)
    gdr_queue_set = xQueueCreateSet(2);
    gdr_set_member_a = xQueueCreateStatic(1, sizeof(uint32_t),
                                          gdr_set_member_a_storage,
                                          &gdr_set_member_a_buf);
    gdr_set_member_b = xQueueCreateStatic(1, sizeof(uint32_t),
                                          gdr_set_member_b_storage,
                                          &gdr_set_member_b_buf);
#endif
#endif

/* Reason: gdr_static_event_group must be genuinely static whenever static
 * allocation exists -- on static-dynamic the mixed-allocation branch used to
 * fall through to a dynamic create, so no object in that variant ever
 * carried ucStaticallyAllocated == 1 (the member itself only exists under
 * this double-on config).  static-dynamic keeps gdr_event_group dynamic, so
 * both values are live on the same lane. */
#if (configSUPPORT_STATIC_ALLOCATION == 1)
    gdr_static_event_group =
        xEventGroupCreateStatic(&gdr_static_event_group_buf);
#elif (configSUPPORT_DYNAMIC_ALLOCATION == 1)
    gdr_static_event_group = xEventGroupCreate();
#endif

    configASSERT(gdr_registered_queue != NULL);
    configASSERT(gdr_unregistered_queue != NULL);
    configASSERT(gdr_empty_queue != NULL);
    configASSERT(gdr_full_queue != NULL);
    configASSERT(gdr_maxdelay_queue != NULL);
    configASSERT(gdr_semaphore != NULL);
    configASSERT(gdr_mutex != NULL);
    configASSERT(gdr_recursive_mutex != NULL);
    configASSERT(gdr_active_timer != NULL);
    configASSERT(gdr_inactive_timer != NULL);
    configASSERT(gdr_stopped_timer != NULL);
    configASSERT(gdr_oneshot_timer != NULL);
    configASSERT(gdr_created_idle_timer != NULL);
    configASSERT(gdr_event_group != NULL);
    configASSERT(gdr_static_event_group != NULL);
    configASSERT(gdr_stream_buffer != NULL);
    configASSERT(gdr_message_buffer != NULL);

    gdr_register(gdr_registered_queue, "gdr_queue");
    gdr_register(gdr_semaphore, "gdr_semaphore");
    gdr_register(gdr_mutex, "gdr_mutex");

#if (GDR_FIXTURE_STREAM_ACTIONS == 1)
    /* Live evidence for the stream-buffer geometry branches (streams.py).
     * All actions run before the scheduler starts, so the ring state at the
     * harness breakpoint is deterministic. */
    {
        uint8_t pool[24];
        /* gdr_stream_buffer: exactly the trigger level (1 byte) -> the
         * TriggerMet >= branch is live on a non-empty buffer. */
        configASSERT(xStreamBufferSend(gdr_stream_buffer, pool, 1, 0) == 1);
        /* gdr_batching_buffer: exactly the trigger level (1 byte) -> the
         * batching > branch (strictly greater) must report no. */
#if (GDR_HAS_BATCHING_BUFFER == 1)
        configASSERT(xStreamBufferSend(gdr_batching_buffer, pool, 1, 0) == 1);
#endif
        /* gdr_message_buffer: three 4-byte messages written, two read,
         * three written again wraps the ring (xHead < xTail) and leaves a
         * valid NextMsg length prefix at xTail. */
        for (uint32_t i = 0; i < 3; i++) {
            configASSERT(xMessageBufferSend(gdr_message_buffer, pool, 4, 0) == 4);
        }
        configASSERT(xMessageBufferReceive(gdr_message_buffer, pool, sizeof(pool),
                                           0) == 4);
        configASSERT(xMessageBufferReceive(gdr_message_buffer, pool, sizeof(pool),
                                           0) == 4);
        for (uint32_t i = 0; i < 3; i++) {
            configASSERT(xMessageBufferSend(gdr_message_buffer, pool, 4, 0) == 4);
        }
        (void)pool;
    }
    gdr_deleted_stream_buffer = xStreamBufferCreateStatic(
        sizeof(gdr_deleted_stream_buffer_storage), 1,
        gdr_deleted_stream_buffer_storage, &gdr_deleted_stream_buffer_buf);
    configASSERT(gdr_deleted_stream_buffer != NULL);
    vStreamBufferDelete(gdr_deleted_stream_buffer);
#endif

#if (configENABLE_MPU == 0)
    {
        uint32_t full_item = 1U;
        configASSERT(xQueueSend(gdr_full_queue, &full_item, 0) == pdPASS);
    }
    configASSERT(xTimerStart(gdr_active_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_stopped_timer, 0) == pdPASS);
    configASSERT(xTimerStop(gdr_stopped_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_oneshot_timer, 0) == pdPASS);
#endif

#if (configUSE_QUEUE_SETS == 1)
    configASSERT(gdr_queue_set != NULL);
    configASSERT(gdr_set_member_a != NULL);
    configASSERT(gdr_set_member_b != NULL);
    configASSERT(xQueueAddToSet(gdr_set_member_a, gdr_queue_set) == pdPASS);
    configASSERT(xQueueAddToSet(gdr_set_member_b, gdr_queue_set) == pdPASS);
#endif

    /* An unregistered event group with no retained handle: the only live
     * evidence of a waiter that cannot be named from the registry. */
#if (configSUPPORT_DYNAMIC_ALLOCATION == 1)
    {
        EventGroupHandle_t hidden = xEventGroupCreate();
        configASSERT(hidden != NULL);
        (void)hidden;
    }
#endif

#if (GDR_FIXTURE_MIXED_ALLOCATION == 1)
    gdr_create_task_static(gdr_ready_task, "gdr_ready", gdr_ready_stack,
                           GDR_STACK_WORDS, 4, &gdr_ready_tcb, &gdr_high_task);
#endif
#if (configSUPPORT_DYNAMIC_ALLOCATION == 1)
#if (GDR_FIXTURE_MIXED_ALLOCATION == 0)
    gdr_create_task(gdr_ready_task, "gdr_ready", configMINIMAL_STACK_SIZE, 4,
                    &gdr_high_task);
#endif
    gdr_create_task(gdr_delay_task, "gdr_normal", configMINIMAL_STACK_SIZE, 2,
                    &gdr_normal_task);
    gdr_create_task(gdr_delay_task, "gdr_low", configMINIMAL_STACK_SIZE, 1,
                    &gdr_low_task);
    gdr_create_task(gdr_queue_recv, "gdr_qrecv", configMINIMAL_STACK_SIZE, 2,
                    &gdr_queue_recv_task);
    gdr_create_task(gdr_queue_send, "gdr_qsend", configMINIMAL_STACK_SIZE, 2,
                    &gdr_queue_send_task);
    gdr_create_task(gdr_sem_take, "gdr_semw", configMINIMAL_STACK_SIZE, 2,
                    &gdr_sem_take_task);
    gdr_create_task(gdr_mutex_hold, "gdr_mtxh", configMINIMAL_STACK_SIZE, 1,
                    &gdr_mutex_hold_task);
    gdr_create_task(gdr_mutex_take, "gdr_mtxw", configMINIMAL_STACK_SIZE, 3,
                    &gdr_mutex_take_task);
    gdr_create_task(gdr_event_wait, "gdr_evw", configMINIMAL_STACK_SIZE, 2,
                    &gdr_event_wait_task);
    gdr_create_task(gdr_notify_wait, "gdr_ntfy", configMINIMAL_STACK_SIZE, 2,
                    &gdr_notify_wait_task);
    gdr_create_task(gdr_maxdelay_recv, "gdr_maxd", configMINIMAL_STACK_SIZE, 2,
                    &gdr_maxdelay_task);
    gdr_create_task(gdr_suspended, "gdr_susp", configMINIMAL_STACK_SIZE, 2,
                    &gdr_suspended_task);
    gdr_create_task(gdr_recursive_hold, "gdr_recm", configMINIMAL_STACK_SIZE, 2,
                    &gdr_recursive_task);
    gdr_create_task(gdr_exhaust_stack_task, "gdr_exh",
                    (uint16_t)GDR_EXHAUST_STACK_WORDS, 2, &gdr_exhaust_task);
    gdr_create_task(gdr_ready_spin, "gdr_spin", configMINIMAL_STACK_SIZE, 0,
                    &gdr_ready_spin_task);
    gdr_create_task(gdr_waiter_only_eg_task, "gdr_egw",
                    configMINIMAL_STACK_SIZE, 2, &gdr_waiter_only_eg_handle);
#if (configENABLE_MPU == 1)
    /* Above the ready task (4) so the priming completes before the 80 ms
     * ready marker; it deletes itself after the one-time sends. */
    gdr_create_task(gdr_mpu_boot_priming, "gdr_bootp", configMINIMAL_STACK_SIZE,
                    5, NULL);
#endif
#if (INCLUDE_xTimerPendFunctionCall == 1)
    gdr_create_task(gdr_timer_command_task, "gdr_tmrcmd", configMINIMAL_STACK_SIZE,
                    2, NULL);
#endif
#if (configNUMBER_OF_CORES > 1)
    gdr_create_task(gdr_bound, "gdr_bound", configMINIMAL_STACK_SIZE, 2,
                    &gdr_bound_task);
    /* Pin to core 1 only: the Affinity column needs a real mask, and leaving
     * core 0 to the primary-path tasks keeps the timing independent of which
     * core wins the scheduler race. */
    vTaskCoreAffinitySet(gdr_bound_task, 0x2U);
    gdr_create_task(gdr_preempt, "gdr_preempt", configMINIMAL_STACK_SIZE, 2,
                    &gdr_preempt_task);
    /* Reason: on two cores the shared ground-truth tasks (blocked waiters,
     * mutex holder, delayers) could each win on a different core depending on
     * the scheduling race, so the blocked graph is not deterministic at
     * snapshot time; a blocked waiter caught mid-yield renders as
     * Running(yielding) and its queue/semaphore waiter count reads 0, which
     * breaks the shared queue-family and state-coverage assertions.  Pinning
     * every such task to core 0 restores the single-core priority +
     * creation-order semantics this fixture's assertions rely on, and because
     * the cross-core doorbell (vInterruptCore) is a weak no-op on this board
     * no shared assertion ever needs a cross-core yield to be delivered.
     * gdr_bound (core 1, genuine Affinity mask) and the two idle tasks keep
     * the lane genuinely dual-core. */
    vTaskCoreAffinitySet(gdr_mutex_hold_task, 0x1U);
    vTaskCoreAffinitySet(gdr_mutex_take_task, 0x1U);
    vTaskCoreAffinitySet(gdr_queue_recv_task, 0x1U);
    vTaskCoreAffinitySet(gdr_queue_send_task, 0x1U);
    vTaskCoreAffinitySet(gdr_sem_take_task, 0x1U);
    vTaskCoreAffinitySet(gdr_event_wait_task, 0x1U);
    vTaskCoreAffinitySet(gdr_notify_wait_task, 0x1U);
    vTaskCoreAffinitySet(gdr_maxdelay_task, 0x1U);
    vTaskCoreAffinitySet(gdr_suspended_task, 0x1U);
    vTaskCoreAffinitySet(gdr_recursive_task, 0x1U);
    vTaskCoreAffinitySet(gdr_exhaust_task, 0x1U);
    vTaskCoreAffinitySet(gdr_ready_spin_task, 0x1U);
    vTaskCoreAffinitySet(gdr_waiter_only_eg_handle, 0x1U);
    vTaskCoreAffinitySet(gdr_normal_task, 0x1U);
    vTaskCoreAffinitySet(gdr_low_task, 0x1U);
    vTaskCoreAffinitySet(gdr_preempt_task, 0x1U);
#endif
#else
    gdr_create_task_static(gdr_ready_task, "gdr_ready", gdr_ready_stack,
                           GDR_STACK_WORDS, 4, &gdr_ready_tcb, &gdr_high_task);
    gdr_create_task_static(gdr_delay_task, "gdr_normal", gdr_normal_stack,
                           GDR_STACK_WORDS, 2, &gdr_normal_tcb,
                           &gdr_normal_task);
    gdr_create_task_static(gdr_delay_task, "gdr_low", gdr_low_stack,
                           GDR_STACK_WORDS, 1, &gdr_low_tcb, &gdr_low_task);
    gdr_create_task_static(gdr_queue_recv, "gdr_qrecv", gdr_queue_recv_stack,
                           GDR_STACK_WORDS, 2, &gdr_queue_recv_tcb,
                           &gdr_queue_recv_task);
    gdr_create_task_static(gdr_queue_send, "gdr_qsend", gdr_queue_send_stack,
                           GDR_STACK_WORDS, 2, &gdr_queue_send_tcb,
                           &gdr_queue_send_task);
    gdr_create_task_static(gdr_sem_take, "gdr_semw", gdr_sem_take_stack,
                           GDR_STACK_WORDS, 2, &gdr_sem_take_tcb,
                           &gdr_sem_take_task);
    gdr_create_task_static(gdr_mutex_hold, "gdr_mtxh", gdr_mutex_hold_stack,
                           GDR_STACK_WORDS, 1, &gdr_mutex_hold_tcb,
                           &gdr_mutex_hold_task);
    gdr_create_task_static(gdr_mutex_take, "gdr_mtxw", gdr_mutex_take_stack,
                           GDR_STACK_WORDS, 3, &gdr_mutex_take_tcb,
                           &gdr_mutex_take_task);
    gdr_create_task_static(gdr_event_wait, "gdr_evw", gdr_event_wait_stack,
                           GDR_STACK_WORDS, 2, &gdr_event_wait_tcb,
                           &gdr_event_wait_task);
    gdr_create_task_static(gdr_notify_wait, "gdr_ntfy", gdr_notify_wait_stack,
                           GDR_STACK_WORDS, 2, &gdr_notify_wait_tcb,
                           &gdr_notify_wait_task);
    gdr_create_task_static(gdr_maxdelay_recv, "gdr_maxd", gdr_maxdelay_stack,
                           GDR_STACK_WORDS, 2, &gdr_maxdelay_tcb,
                           &gdr_maxdelay_task);
    gdr_create_task_static(gdr_suspended, "gdr_susp", gdr_suspended_stack,
                           GDR_STACK_WORDS, 2, &gdr_suspended_tcb,
                           &gdr_suspended_task);
    gdr_create_task_static(gdr_recursive_hold, "gdr_recm", gdr_recursive_stack,
                           GDR_STACK_WORDS, 2, &gdr_recursive_tcb,
                           &gdr_recursive_task);
    gdr_create_task_static(gdr_exhaust_stack_task, "gdr_exh",
                           gdr_exhaust_stack_mem, GDR_EXHAUST_STACK_WORDS, 2,
                           &gdr_exhaust_tcb, &gdr_exhaust_task);
    gdr_create_task_static(gdr_ready_spin, "gdr_spin", gdr_ready_spin_stack,
                           GDR_STACK_WORDS, 0, &gdr_ready_spin_tcb,
                           &gdr_ready_spin_task);
    gdr_create_task_static(gdr_waiter_only_eg_task, "gdr_egw",
                           gdr_waiter_only_eg_stack, GDR_STACK_WORDS, 2,
                           &gdr_waiter_only_eg_tcb, &gdr_waiter_only_eg_handle);
#endif

    vTaskStartScheduler();
    GDR_FIXTURE_UNREACHABLE();
    return 0;
}
