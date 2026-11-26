/* Deterministic scheduler-transition fixture for the FreeRTOS ARM_CM0 port.
 *
 * Uses nRF51 TIMER0 (IRQ 8) and TIMER1 (IRQ 9) on QEMU microbit.
 */
#include "FreeRTOS.h"
#include "task.h"

#if defined(GDR_FIXTURE_SCHED_TESTS) && (GDR_FIXTURE_SCHED_TESTS == 1)

#include <stdint.h>

#define GDR_SCHED_REG(base, offset) (*(volatile uint32_t *)((base) + (offset)))
#define GDR_SCHED_TIMER0_BASE 0x40008000UL
#define GDR_SCHED_TIMER1_BASE 0x40009000UL
#define GDR_SCHED_NVIC_ISER GDR_SCHED_REG(0xE000E100UL, 0)
#define GDR_SCHED_NVIC_ICPR GDR_SCHED_REG(0xE000E280UL, 0)
#define GDR_SCHED_NVIC_IPR ((volatile uint8_t *)0xE000E400UL)

#define GDR_SCHED_IRQ_TIMER0 8U
#define GDR_SCHED_IRQ_TIMER1 9U

enum {
    GDR_SCHED_GATE_YIELD = 1,
    GDR_SCHED_GATE_TICK = 2,
    GDR_SCHED_GATE_IRQ = 3,
};

volatile uint32_t gdr_sched_gate;
volatile uint32_t gdr_sched_done;
volatile uint32_t gdr_sched_phase;
volatile uint32_t gdr_sched_irq_requested;
volatile uint32_t gdr_sched_timer1_done;
volatile uint32_t gdr_sched_first_started;
volatile uint32_t gdr_sched_yield_count;

extern TaskHandle_t volatile pxCurrentTCB;

TaskHandle_t gdr_sched_first_handle;
TaskHandle_t gdr_sched_task_handle;
TaskHandle_t gdr_sched_tick_handle;
TaskHandle_t gdr_sched_irq_handle;

static void gdr_sched_timer_stop(uint32_t base)
{
    GDR_SCHED_REG(base, 0x004) = 1;
    GDR_SCHED_REG(base, 0x140) = 0;
}

static void gdr_sched_timer_arm(uint32_t base, uint32_t count)
{
    gdr_sched_timer_stop(base);
    GDR_SCHED_REG(base, 0x00C) = 1;
    GDR_SCHED_REG(base, 0x504) = 0;
    GDR_SCHED_REG(base, 0x508) = 3;
    GDR_SCHED_REG(base, 0x510) = 4;
    GDR_SCHED_REG(base, 0x540) = count;
    GDR_SCHED_REG(base, 0x304) = 1U << 16;
    GDR_SCHED_REG(base, 0x000) = 1;
}

static void gdr_sched_irq_hw_init(void)
{
    gdr_sched_timer_stop(GDR_SCHED_TIMER0_BASE);
    gdr_sched_timer_stop(GDR_SCHED_TIMER1_BASE);
    GDR_SCHED_NVIC_ICPR =
        (1U << GDR_SCHED_IRQ_TIMER0) | (1U << GDR_SCHED_IRQ_TIMER1);
    GDR_SCHED_NVIC_IPR[GDR_SCHED_IRQ_TIMER0] = 0xC0U;
    GDR_SCHED_NVIC_IPR[GDR_SCHED_IRQ_TIMER1] = 0x40U;
    GDR_SCHED_NVIC_ISER =
        (1U << GDR_SCHED_IRQ_TIMER0) | (1U << GDR_SCHED_IRQ_TIMER1);
}

__attribute__((noinline, used)) static void gdr_sched_first_task(void *argument)
{
    (void)argument;
    gdr_sched_first_started = 1;
    vTaskSuspend(NULL);
    for (;;) {
    }
}

__attribute__((noinline, used)) static void gdr_sched_tick_hi_task(void *argument)
{
    (void)argument;
    for (;;) {
        vTaskDelay(5);
    }
}

__attribute__((noinline, used)) static void gdr_sched_irq_hi_task(void *argument)
{
    (void)argument;
    for (;;) {
        (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    }
}

void gdr_sched_timer1_isr(void)
{
    gdr_sched_timer_stop(GDR_SCHED_TIMER1_BASE);
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(gdr_sched_irq_handle, &woken);
    gdr_sched_irq_requested = 1;
    gdr_sched_timer1_done = 1;
    portYIELD_FROM_ISR(woken);
}

BaseType_t gdr_sched_timer0_begin(void)
{
    gdr_sched_timer_stop(GDR_SCHED_TIMER0_BASE);
    if (pxCurrentTCB != gdr_sched_task_handle) {
        gdr_sched_timer_arm(GDR_SCHED_TIMER0_BASE, 2000U);
        return pdFALSE;
    }
    gdr_sched_timer1_done = 0;
    gdr_sched_timer_arm(GDR_SCHED_TIMER1_BASE, 500U);
    return pdTRUE;
}

void gdr_sched_timer0_end(void)
{
    gdr_sched_timer_stop(GDR_SCHED_TIMER1_BASE);
}

__attribute__((noinline, used)) void gdr_sched_cycle_done(void)
{
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) static void gdr_sched_task(void *argument)
{
    (void)argument;
    gdr_sched_irq_hw_init();
    for (;;) {
        uint32_t gate = gdr_sched_gate;
        if (gate == 0) {
            vTaskDelay(1);
            continue;
        }
        gdr_sched_done = 0;
        gdr_sched_irq_requested = 0;
        gdr_sched_phase = gate;
        if (gate == GDR_SCHED_GATE_YIELD) {
            gdr_sched_yield_count++;
            taskYIELD();
        } else if (gate == GDR_SCHED_GATE_TICK) {
            for (volatile uint32_t spin = 0;
                 spin < 20000000U && gdr_sched_gate == gate; spin++) {
            }
        } else if (gate == GDR_SCHED_GATE_IRQ) {
            vTaskSuspend(gdr_sched_tick_handle);
            gdr_sched_timer_arm(GDR_SCHED_TIMER0_BASE, 2000U);
            while (!gdr_sched_irq_requested && gdr_sched_gate == gate) {
                __asm__ volatile("" ::: "memory");
            }
            gdr_sched_timer_stop(GDR_SCHED_TIMER0_BASE);
            vTaskResume(gdr_sched_tick_handle);
        }
        gdr_sched_done = gate;
        gdr_sched_gate = 0;
        gdr_sched_phase = 0;
        gdr_sched_irq_requested = 0;
        gdr_sched_cycle_done();
    }
}

void gdr_sched_fixture_start(void)
{
    BaseType_t ok = xTaskCreate(gdr_sched_first_task, "gdr_sched_first", 192,
                                NULL, 5, &gdr_sched_first_handle);
    configASSERT(ok == pdPASS);
    ok = xTaskCreate(gdr_sched_irq_hi_task, "gdr_sched_irq", 192, NULL, 4,
                     &gdr_sched_irq_handle);
    configASSERT(ok == pdPASS);
    ok = xTaskCreate(gdr_sched_tick_hi_task, "gdr_sched_tick", 192, NULL, 4,
                     &gdr_sched_tick_handle);
    configASSERT(ok == pdPASS);
    ok = xTaskCreate(gdr_sched_task, "gdr_sched", 192, NULL, 3,
                     &gdr_sched_task_handle);
    configASSERT(ok == pdPASS);
}

#endif
