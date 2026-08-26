/* Shared FreeRTOSConfig knobs for every GDR fixture variant.
 *
 * Variant headers include this file after setting their deltas. Keep the
 * defaults identical to the historical B-L475E-IOT01A / 10.3.1 combination
 * so the `base` variant reproduces the original assertions.
 */
#ifndef GDR_FIXTURE_COMMON_H
#define GDR_FIXTURE_COMMON_H

#include <stdint.h>

#ifndef configUSE_PREEMPTION
#define configUSE_PREEMPTION 1
#endif
#ifndef configUSE_IDLE_HOOK
#define configUSE_IDLE_HOOK 0
#endif
#ifndef configUSE_TICK_HOOK
#define configUSE_TICK_HOOK 0
#endif
#ifndef configTICK_RATE_HZ
#define configTICK_RATE_HZ ((TickType_t)1000)
#endif
#ifndef configMAX_PRIORITIES
#define configMAX_PRIORITIES 6
#endif
#ifndef configMINIMAL_STACK_SIZE
#define configMINIMAL_STACK_SIZE ((uint16_t)128)
#endif
#ifndef configTOTAL_HEAP_SIZE
/* Reason: the expanded waiter/object fixture needs more than the original
 * 20 KiB (3 tasks + 2 queues). 48 KiB still fits the 96 KiB SRAM. */
#define configTOTAL_HEAP_SIZE ((size_t)(48 * 1024))
#endif
#ifndef configMAX_TASK_NAME_LEN
#define configMAX_TASK_NAME_LEN 16
#endif
#ifndef configUSE_TRACE_FACILITY
#define configUSE_TRACE_FACILITY 1
#endif
#ifndef configUSE_16_BIT_TICKS
#define configUSE_16_BIT_TICKS 0
#endif
#ifndef configIDLE_SHOULD_YIELD
#define configIDLE_SHOULD_YIELD 1
#endif
#ifndef configUSE_MUTEXES
#define configUSE_MUTEXES 1
#endif
#ifndef configUSE_RECURSIVE_MUTEXES
#define configUSE_RECURSIVE_MUTEXES 1
#endif
#ifndef configUSE_COUNTING_SEMAPHORES
#define configUSE_COUNTING_SEMAPHORES 1
#endif
#ifndef configQUEUE_REGISTRY_SIZE
#define configQUEUE_REGISTRY_SIZE 8
#endif
#ifndef configCHECK_FOR_STACK_OVERFLOW
#define configCHECK_FOR_STACK_OVERFLOW 0
#endif
#ifndef configUSE_MALLOC_FAILED_HOOK
#define configUSE_MALLOC_FAILED_HOOK 1
#endif
#ifndef configUSE_APPLICATION_TASK_TAG
#define configUSE_APPLICATION_TASK_TAG 0
#endif
#ifndef configUSE_PORT_OPTIMISED_TASK_SELECTION
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1
#endif
#ifndef configUSE_TASK_NOTIFICATIONS
#define configUSE_TASK_NOTIFICATIONS 1
#endif
#ifndef configSUPPORT_DYNAMIC_ALLOCATION
#define configSUPPORT_DYNAMIC_ALLOCATION 1
#endif
#ifndef configSUPPORT_STATIC_ALLOCATION
#define configSUPPORT_STATIC_ALLOCATION 0
#endif

#ifndef configUSE_TIMERS
#define configUSE_TIMERS 1
#endif
#ifndef configTIMER_TASK_PRIORITY
#define configTIMER_TASK_PRIORITY 3
#endif
#ifndef configTIMER_QUEUE_LENGTH
#define configTIMER_QUEUE_LENGTH 8
#endif
#ifndef configTIMER_TASK_STACK_DEPTH
#define configTIMER_TASK_STACK_DEPTH (configMINIMAL_STACK_SIZE * 2)
#endif

#ifndef INCLUDE_vTaskPrioritySet
#define INCLUDE_vTaskPrioritySet 1
#endif
#ifndef INCLUDE_uxTaskPriorityGet
#define INCLUDE_uxTaskPriorityGet 1
#endif
#ifndef INCLUDE_vTaskDelete
#define INCLUDE_vTaskDelete 1
#endif
#ifndef INCLUDE_vTaskSuspend
#define INCLUDE_vTaskSuspend 1
#endif
#ifndef INCLUDE_vTaskDelay
#define INCLUDE_vTaskDelay 1
#endif
#ifndef INCLUDE_xTaskGetSchedulerState
#define INCLUDE_xTaskGetSchedulerState 1
#endif
#ifndef INCLUDE_xTaskGetCurrentTaskHandle
#define INCLUDE_xTaskGetCurrentTaskHandle 1
#endif

#ifndef configPRIO_BITS
#define configPRIO_BITS 4
#endif
#ifndef configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY 15
#endif
#ifndef configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 5
#endif
#ifndef configKERNEL_INTERRUPT_PRIORITY
#define configKERNEL_INTERRUPT_PRIORITY \
    (configLIBRARY_LOWEST_INTERRUPT_PRIORITY << (8 - configPRIO_BITS))
#endif
#ifndef configMAX_SYSCALL_INTERRUPT_PRIORITY
#define configMAX_SYSCALL_INTERRUPT_PRIORITY \
    (configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY << (8 - configPRIO_BITS))
#endif

#ifndef vPortSVCHandler
#define vPortSVCHandler SVC_Handler
#endif
#ifndef xPortPendSVHandler
#define xPortPendSVHandler PendSV_Handler
#endif

#ifndef configASSERT
void gdr_fixture_assert_failed(int line);
#define configASSERT(value)                  \
    do {                                     \
        if (!(value)) {                      \
            gdr_fixture_assert_failed(__LINE__); \
        }                                    \
    } while (0)
#endif

#endif /* GDR_FIXTURE_COMMON_H */
