/* Deterministic synchronous-fault fixture for the FreeRTOS CM33 port.
 *
 * A low-priority task cycles two fault scenarios on mps2-an505.  UsageFault
 * stays disabled so an undefined instruction escalates to HardFault
 * (FORCED + UFSR UNDEFINSTR); and a data load from the unmapped 0xA0000000
 * address raises a precise BusFault (escalated to HardFault when BusFault
 * remains disabled, reporting BFSR PRECISERR).
 */
#include "FreeRTOS.h"
#include "task.h"

#include <stdint.h>

#if defined(GDR_FIXTURE_FAULT_TESTS) && (GDR_FIXTURE_FAULT_TESTS == 1)

#define GDR_SCB_CFSR (*(volatile uint32_t *)0xE000ED28UL)
#define GDR_SCB_HFSR (*(volatile uint32_t *)0xE000ED2CUL)

static volatile uint32_t gdr_fault_done;
static volatile uint32_t gdr_fault_scenario;

enum {
    GDR_FAULT_SCENARIO_UNDEFINSTR = 0,
    GDR_FAULT_SCENARIO_BUS = 1,
};

__attribute__((noinline, used)) void gdr_fault_undefinstr_leaf(void)
{
    __asm volatile(".global gdr_fault_ud_stop\ngdr_fault_ud_stop:\nnop" ::: "memory");
    __asm__ volatile(".word 0xFFFFFFFF");
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_fault_bus_leaf(void)
{
    __asm volatile(".global gdr_fault_bus_stop\ngdr_fault_bus_stop:\nnop" ::: "memory");
    __asm__ volatile(
        "ldr r0, =0xA0000000\n"
        "ldr.n r1, [r0]\n"
        ::: "r0", "r1", "memory");
}

__attribute__((noinline, used)) static uint32_t *gdr_fault_frame(uint32_t exc_return)
{
    uint32_t *frame;

    if (exc_return & 4U) {
        __asm__ volatile("mrs %0, psp" : "=r"(frame)::"memory");
    } else {
        __asm__ volatile("mrs %0, msp" : "=r"(frame)::"memory");
    }
    return frame;
}

void HardFault_Handler(void)
{
    uint32_t exc_return;
    __asm__ volatile("mov %0, lr" : "=r"(exc_return)::"memory");
    uint32_t *frame = gdr_fault_frame(exc_return);

    frame[6] += gdr_fault_scenario == GDR_FAULT_SCENARIO_UNDEFINSTR ? 4 : 2;
    GDR_SCB_CFSR = GDR_SCB_CFSR;
    GDR_SCB_HFSR = GDR_SCB_HFSR;
    gdr_fault_done++;
}

static void gdr_fault_task(void *argument)
{
    (void)argument;
    vTaskDelay(pdMS_TO_TICKS(150));
    for (;;) {
        gdr_fault_scenario = GDR_FAULT_SCENARIO_UNDEFINSTR;
        gdr_fault_undefinstr_leaf();
        vTaskDelay(pdMS_TO_TICKS(100));
        gdr_fault_scenario = GDR_FAULT_SCENARIO_BUS;
        gdr_fault_bus_leaf();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void gdr_fault_fixture_start(void)
{
    configASSERT(xTaskCreate(gdr_fault_task, "gdr_fault", 192, NULL, 2, NULL) ==
                 pdPASS);
}

#endif
