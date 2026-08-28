/* Shared GDR FreeRTOS QEMU fixture.
 *
 * Variant differences are selected by FreeRTOSConfig.h knobs
 * (configSUPPORT_*_ALLOCATION, configUSE_QUEUE_SETS, configUSE_TRACE_FACILITY,
 * tskKERNEL_VERSION_*). Heap_5 region setup is gated by GDR_FIXTURE_HEAP_5.
 */
#include "FreeRTOS.h"
#include "event_groups.h"
#include "queue.h"
#include "semphr.h"
#include "stream_buffer.h"
#include "task.h"
#include "timers.h"

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

/* Packed decimal encoding of the kernel version for CU-independent probing. */
GDR_USED __attribute__((section(".rodata.gdr_version")))
const uint32_t gdr_freertos_version_num =
    (uint32_t)tskKERNEL_VERSION_MAJOR * 10000U +
    (uint32_t)tskKERNEL_VERSION_MINOR * 100U +
    (uint32_t)tskKERNEL_VERSION_BUILD;

extern void xPortSysTickHandler(void);
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
#endif

static void gdr_semihosting_write0(const char *message)
{
    register int operation __asm__("r0") = 0x04;
    register const char *argument __asm__("r1") = message;
    __asm__ volatile("bkpt 0xab" : : "r"(operation), "r"(argument) : "memory");
}

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

/* Reason: the ready marker used to fire after a 20 ms delay, before every
 * extra waiter had entered its stable blocked/suspended state. Stretch the
 * delay so QEMU's boot wait sees a deterministic object graph. */
static void gdr_ready_task(void *argument)
{
    (void)argument;
    vTaskDelay(pdMS_TO_TICKS(80));
    gdr_semihosting_write0("GDR FreeRTOS fixture ready.\n");
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void SysTick_Handler(void)
{
    xPortSysTickHandler();
}

#if (configUSE_TICK_HOOK == 1)
void vApplicationTickHook(void)
{
    gdr_runtime_timer_tick();
}
#endif

#if (configSUPPORT_DYNAMIC_ALLOCATION == 1)
static TaskHandle_t gdr_create_task(TaskFunction_t fn, const char *name,
                                    uint16_t stack, UBaseType_t prio,
                                    TaskHandle_t *out)
{
    configASSERT(xTaskCreate(fn, name, stack, NULL, prio, out) == pdPASS);
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
 * guaranteed link order -- the b-l475e build placed the second array below
 * the first and the fixture died in that configASSERT before reaching
 * vTaskStartScheduler -- so both regions come from one ordered object. */
static struct {
    uint8_t low[32 * 1024];
    uint8_t high[16 * 1024];
} gdr_heap_arena __attribute__((aligned(8)));

static void gdr_init_heap_regions(void)
{
    /* Two live regions plus a trailing 0-size terminator (heap_5 walks
     * until xSizeInBytes == 0). The zero-size slot terminates the region list. */
    const HeapRegion_t regions[] = {
        {gdr_heap_arena.low, sizeof(gdr_heap_arena.low)},
        {gdr_heap_arena.high, sizeof(gdr_heap_arena.high)},
        {NULL, 0},
    };
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
    gdr_active_timer = xTimerCreate("gdr_active", pdMS_TO_TICKS(100), pdTRUE,
                                    NULL, gdr_timer_callback);
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

#if (configSUPPORT_STATIC_ALLOCATION == 1) && \
    (GDR_FIXTURE_MIXED_ALLOCATION == 0)
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

    {
        uint32_t full_item = 1U;
        configASSERT(xQueueSend(gdr_full_queue, &full_item, 0) == pdPASS);
    }
    configASSERT(xTimerStart(gdr_active_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_stopped_timer, 0) == pdPASS);
    configASSERT(xTimerStop(gdr_stopped_timer, 0) == pdPASS);
    configASSERT(xTimerStart(gdr_oneshot_timer, 0) == pdPASS);

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
