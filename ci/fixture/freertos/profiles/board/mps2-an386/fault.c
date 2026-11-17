/* Deterministic synchronous-fault fixture for the FreeRTOS M4 port.
 *
 * A low-priority task cycles three fault scenarios. UsageFault stays disabled
 * so an undefined instruction escalates to HardFault (FORCED + UFSR
 * UNDEFINSTR), which GDR reports as a precise synchronous fault; a data load
 * from an unmapped address raises a precise BusFault when the bus answers the
 * error synchronously; and a no-access MPU region makes a data load raise a
 * direct MemManage fault (DACCVIOL + MMARVALID). IACCVIOL stays synthetic and
 * unit covered: an execute-never branch target does not raise the specified
 * direct fault on the QEMU MPS2 model, and a test fixture must never contain
 * a path that can wedge the shared halted session. Each handler advances the
 * stacked PC past the faulting instruction and clears its status bits.
 */
#include "FreeRTOS.h"
#include "task.h"

#include <stdint.h>

#if defined(GDR_FIXTURE_FAULT_TESTS) && (GDR_FIXTURE_FAULT_TESTS == 1)

#define GDR_SCB_CFSR (*(volatile uint32_t *)0xE000ED28UL)
#define GDR_SCB_HFSR (*(volatile uint32_t *)0xE000ED2CUL)
#define GDR_SCB_SHCSR (*(volatile uint32_t *)0xE000ED24UL)
#define GDR_MPU_CTRL (*(volatile uint32_t *)0xE000ED94UL)
#define GDR_MPU_RNR (*(volatile uint32_t *)0xE000ED98UL)
#define GDR_MPU_RBAR (*(volatile uint32_t *)0xE000ED9CUL)
#define GDR_MPU_RASR (*(volatile uint32_t *)0xE000EDA0UL)

#define GDR_FAULT_BUS_ADDR 0x50000000UL
#define GDR_FAULT_MPU_ADDR 0x60000000UL

static volatile uint32_t gdr_fault_done;
static volatile uint32_t gdr_fault_scenario;
volatile uint32_t gdr_fault_gate;

enum {
    GDR_FAULT_SCENARIO_UNDEFINSTR = 0,
    GDR_FAULT_SCENARIO_BUS = 1,
    GDR_FAULT_SCENARIO_MEMMANAGE = 2,
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
        "ldr r0, =0x50000000\n"
        "ldr r1, [r0]\n"
        ::: "r0", "r1", "memory");
}

__attribute__((noinline, used)) void gdr_fault_memmanage_leaf(void)
{
    __asm volatile(".global gdr_fault_mm_stop\ngdr_fault_mm_stop:\nnop" ::: "memory");
    __asm__ volatile(
        "ldr r0, =0x60000000\n"
        "ldr r1, [r0]\n"
        ::: "r0", "r1", "memory");
}

static uint32_t *gdr_fault_frame(uint32_t exc_return)
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

void MemManage_Handler(void)
{
    uint32_t exc_return;
    __asm__ volatile("mov %0, lr" : "=r"(exc_return)::"memory");
    uint32_t *frame = gdr_fault_frame(exc_return);

    frame[6] += 2;
    GDR_SCB_CFSR = GDR_SCB_CFSR;
    gdr_fault_done++;
}

static void gdr_fault_mpu_init(void)
{
    GDR_MPU_RNR = 0;
    GDR_MPU_RBAR = GDR_FAULT_MPU_ADDR;
    GDR_MPU_RASR = (19U << 1) | (1U << 28) | 1U;
    GDR_MPU_CTRL = (1U << 0) | (1U << 2);
    GDR_SCB_SHCSR |= 1U << 16;
    __asm__ volatile("dsb\n isb\n" ::: "memory");
}

static void gdr_fault_task(void *argument)
{
    (void)argument;
    for (;;) {
        if (gdr_fault_gate == 0U) {
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        gdr_fault_scenario = GDR_FAULT_SCENARIO_UNDEFINSTR;
        gdr_fault_undefinstr_leaf();
        vTaskDelay(pdMS_TO_TICKS(100));
        gdr_fault_scenario = GDR_FAULT_SCENARIO_BUS;
        gdr_fault_bus_leaf();
        vTaskDelay(pdMS_TO_TICKS(100));
        gdr_fault_scenario = GDR_FAULT_SCENARIO_MEMMANAGE;
        gdr_fault_memmanage_leaf();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void gdr_fault_fixture_start(void)
{
    gdr_fault_mpu_init();
    configASSERT(xTaskCreate(gdr_fault_task, "gdr_fault", 192, NULL, 2, NULL) ==
                 pdPASS);
}

#endif
