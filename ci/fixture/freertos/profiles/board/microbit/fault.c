/* Deterministic HardFault fixture for the Cortex-M0 port.
 *
 * Armv6-M implements no configurable fault status registers, so every fault
 * surfaces as an unclassified HardFault. The task executes a 2-byte UDF; the
 * handler advances the stacked PC past it and returns, keeping the session
 * healthy. GDR must refuse the frame as fault-frame-imprecise.
 */
#include "FreeRTOS.h"
#include "task.h"

#include <stdint.h>

#if defined(GDR_FIXTURE_FAULT_TESTS) && (GDR_FIXTURE_FAULT_TESTS == 1)

static volatile uint32_t gdr_fault_done;
volatile uint32_t gdr_fault_gate;

__attribute__((noinline, used)) void gdr_fault_udf_leaf(void)
{
    __asm__ volatile("udf #0");
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_fault_trigger(void)
{
    gdr_fault_udf_leaf();
    __asm__ volatile("" ::: "memory");
}

void HardFault_Handler(void)
{
    uint32_t *frame;
    uint32_t lr_value;

    __asm__ volatile("mov %0, lr" : "=r"(lr_value)::"memory");
    if (lr_value & 4U) {
        __asm__ volatile("mrs %0, psp" : "=r"(frame)::"memory");
    } else {
        __asm__ volatile("mrs %0, msp" : "=r"(frame)::"memory");
    }
    frame[6] += 2;
    gdr_fault_done++;
}

static void gdr_fault_task(void *argument)
{
    (void)argument;
    for (;;) {
        if (gdr_fault_gate == 0U) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        gdr_fault_trigger();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void gdr_fault_fixture_start(void)
{
    configASSERT(xTaskCreate(gdr_fault_task, "gdr_fault", 192, NULL, 2, NULL) ==
                 pdPASS);
}

#endif
