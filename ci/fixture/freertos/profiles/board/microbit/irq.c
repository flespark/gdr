/* nRF51 TIMER0/1 hardware exceptions for the Cortex-M0 fixture. */
#include "FreeRTOS.h"
#include "task.h"

#include <stdint.h>

#define REG(base, offset) (*(volatile uint32_t *)((base) + (offset)))
#define TIMER0 0x40008000UL
#define TIMER1 0x40009000UL
#define NVIC_ISER REG(0xE000E100UL, 0)
#define NVIC_ICPR REG(0xE000E280UL, 0)
#define NVIC_IPR ((volatile uint8_t *)0xE000E400UL)

static volatile uint32_t gdr_irq_scenario;
static volatile uint32_t gdr_irq_done;
static volatile uint32_t gdr_irq_nested_seen;

enum {
    GDR_IRQ_SCENARIO_SINGLE = 0,
    GDR_IRQ_SCENARIO_NESTED = 1,
    GDR_IRQ_SCENARIO_ALIGN = 2,
};

static void timer_stop(uint32_t base)
{
    REG(base, 0x004) = 1;
    REG(base, 0x140) = 0;
}

static void timer_arm(uint32_t base, uint32_t count)
{
    timer_stop(base);
    REG(base, 0x00C) = 1;
    REG(base, 0x504) = 0;
    REG(base, 0x508) = 3;
    REG(base, 0x510) = 4;
    REG(base, 0x540) = count;
    REG(base, 0x304) = 1U << 16;
    REG(base, 0x000) = 1;
}

__attribute__((noinline, used)) void gdr_irq_single_leaf(void)
{
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_irq_nested_leaf(void)
{
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_irq_align_leaf(void)
{
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_irq_nested_wait(void)
{
    while (!gdr_irq_nested_seen) {
        __asm__ volatile("" ::: "memory");
    }
}

__attribute__((noinline, used)) void gdr_irq1_body(void)
{
    timer_stop(TIMER1);
    gdr_irq_nested_leaf();
    gdr_irq_nested_seen = 1;
}

void Timer1_Handler(void)
{
    gdr_irq1_body();
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_irq0_body(void)
{
    timer_stop(TIMER0);
    switch (gdr_irq_scenario) {
    case GDR_IRQ_SCENARIO_SINGLE:
        gdr_irq_single_leaf();
        gdr_irq_done = 1;
        break;
    case GDR_IRQ_SCENARIO_NESTED:
        timer_arm(TIMER1, 2500U);
        gdr_irq_nested_wait();
        timer_stop(TIMER1);
        gdr_irq_done = 1;
        break;
    case GDR_IRQ_SCENARIO_ALIGN:
        gdr_irq_align_leaf();
        gdr_irq_done = 1;
        break;
    default:
        break;
    }
}

void Timer0_Handler(void)
{
    gdr_irq0_body();
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) static void gdr_irq_wait(void)
{
    while (!gdr_irq_done) {
        __asm__ volatile("" ::: "memory");
    }
}

__attribute__((naked, noinline, used)) static void gdr_irq_misaligned_wait(void)
{
    __asm__ volatile(
        "sub sp, sp, #4\n"
        ".cfi_def_cfa_offset 4\n"
        "1:\n"
        "ldr r0, =gdr_irq_done\n"
        "ldr r1, [r0]\n"
        "cmp r1, #0\n"
        "beq 1b\n"
        "add sp, sp, #4\n"
        ".cfi_def_cfa_offset 0\n"
        "bx lr\n"
        ".ltorg\n");
}

static void gdr_irq_task(void *argument)
{
    (void)argument;
    timer_stop(TIMER0);
    timer_stop(TIMER1);
    NVIC_ICPR = (1U << 8) | (1U << 9);
    NVIC_IPR[8] = 0xC0;
    NVIC_IPR[9] = 0x40;
    NVIC_ISER = (1U << 8) | (1U << 9);
    vTaskDelay(pdMS_TO_TICKS(150));
    for (;;) {
        gdr_irq_scenario = GDR_IRQ_SCENARIO_SINGLE;
        gdr_irq_done = 0;
        timer_arm(TIMER0, 12500U);
        gdr_irq_wait();
        vTaskDelay(pdMS_TO_TICKS(100));

        gdr_irq_scenario = GDR_IRQ_SCENARIO_NESTED;
        gdr_irq_done = 0;
        gdr_irq_nested_seen = 0;
        timer_arm(TIMER0, 12500U);
        gdr_irq_wait();
        vTaskDelay(pdMS_TO_TICKS(100));

        gdr_irq_scenario = GDR_IRQ_SCENARIO_ALIGN;
        gdr_irq_done = 0;
        timer_arm(TIMER0, 12500U);
        gdr_irq_misaligned_wait();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void gdr_irq_fixture_start(void)
{
    configASSERT(xTaskCreate(gdr_irq_task, "gdr_irq", 192, NULL, 4, NULL) == pdPASS);
}
