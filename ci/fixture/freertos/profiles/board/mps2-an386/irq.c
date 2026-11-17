/* Deterministic CMSDK timer IRQs for the FreeRTOS multi-stack fixture.
 *
 * QEMU's mps2-an385 maps APB timer 0 at 0x40000000 (IRQ 8) and timer 1 at
 * 0x40001000 (IRQ 9).  Timer 0 runs at the kernel's own priority so it can
 * never nest inside SysTick/PendSV; timer 1 has a higher priority and is
 * started from the timer-0 handler to produce one proven nested exception.
 */
#include "FreeRTOS.h"
#include "task.h"

#if defined(GDR_FIXTURE_IRQ_TESTS) && (GDR_FIXTURE_IRQ_TESTS == 1)

#include <stdint.h>

#define GDR_IRQ_TIMER0_BASE 0x40000000UL
#define GDR_IRQ_TIMER1_BASE 0x40001000UL
#define GDR_IRQ_TIMER_ENABLE 0x1U
#define GDR_IRQ_TIMER_IRQEN 0x8U

#define GDR_NVIC_ISER (*(volatile uint32_t *)0xE000E100UL)
#define GDR_NVIC_ICPR (*(volatile uint32_t *)0xE000E280UL)
#define GDR_NVIC_IPR ((volatile uint8_t *)0xE000E400UL)

#define GDR_IRQ_TIMER0 8U
#define GDR_IRQ_TIMER1 9U

typedef struct {
    volatile uint32_t ctrl;
    volatile uint32_t value;
    volatile uint32_t reload;
    volatile uint32_t intstatus;
} gdr_cmsdk_timer_t;

static gdr_cmsdk_timer_t *const gdr_timer0 =
    (gdr_cmsdk_timer_t *)GDR_IRQ_TIMER0_BASE;
static gdr_cmsdk_timer_t *const gdr_timer1 =
    (gdr_cmsdk_timer_t *)GDR_IRQ_TIMER1_BASE;

static volatile float gdr_irq_fp_sink;
volatile uint32_t gdr_irq_gate;

#if defined(GDR_FIXTURE_SCHED_TESTS) && (GDR_FIXTURE_SCHED_TESTS == 1)
extern volatile uint32_t gdr_sched_gate;
extern volatile uint32_t gdr_sched_timer1_done;
extern void gdr_sched_timer0_begin(void);
extern void gdr_sched_timer0_end(void);
extern void gdr_sched_timer1_isr(void);
#endif

static inline void gdr_irq_fpca_write(int set)
{
    uint32_t control;
    __asm__ volatile("mrs %0, control" : "=r"(control) :: "memory");
    if (set)
        control |= 1U << 2;
    else
        control &= ~(1U << 2);
    __asm__ volatile("msr control, %0" ::"r"(control) : "memory");
}

static volatile uint32_t gdr_irq_scenario;
static volatile uint32_t gdr_irq_done;
static volatile uint32_t gdr_irq_nested_seen;

enum {
    GDR_IRQ_SCENARIO_SINGLE = 0,
    GDR_IRQ_SCENARIO_NESTED = 1,
    GDR_IRQ_SCENARIO_ALIGN = 2,
    GDR_IRQ_SCENARIO_FP = 3,
};

static void gdr_irq_timer_stop(gdr_cmsdk_timer_t *timer)
{
    timer->ctrl = 0;
    timer->intstatus = 1;
}

static void gdr_irq_timer_arm(gdr_cmsdk_timer_t *timer, uint32_t reload)
{
    timer->ctrl = 0;
    timer->intstatus = 1;
    timer->reload = reload;
    timer->value = reload;
    timer->ctrl = GDR_IRQ_TIMER_ENABLE | GDR_IRQ_TIMER_IRQEN;
}

static void gdr_irq_hw_init(void)
{
    gdr_irq_timer_stop(gdr_timer0);
    gdr_irq_timer_stop(gdr_timer1);
    GDR_NVIC_ICPR = (1U << GDR_IRQ_TIMER0) | (1U << GDR_IRQ_TIMER1);
    GDR_NVIC_IPR[GDR_IRQ_TIMER0] = 0xFFU;
    GDR_NVIC_IPR[GDR_IRQ_TIMER1] = 0x40U;
    GDR_NVIC_ISER = (1U << GDR_IRQ_TIMER0) | (1U << GDR_IRQ_TIMER1);
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

__attribute__((noinline, used)) void gdr_irq_fp_leaf(void)
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
    gdr_irq_nested_leaf();
    gdr_irq_timer_stop(gdr_timer1);
    gdr_irq_nested_seen = 1;
}

void Timer1_Handler(void)
{
#if defined(GDR_FIXTURE_SCHED_TESTS) && (GDR_FIXTURE_SCHED_TESTS == 1)
    if (gdr_sched_gate == 3U) {
        gdr_sched_timer1_isr();
        return;
    }
#endif
    gdr_irq1_body();
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) void gdr_irq0_body(void)
{
    gdr_irq_timer_stop(gdr_timer0);
    switch (gdr_irq_scenario) {
    case GDR_IRQ_SCENARIO_SINGLE:
        gdr_irq_single_leaf();
        gdr_irq_done = 1;
        break;
    case GDR_IRQ_SCENARIO_NESTED:
        gdr_irq_timer_arm(gdr_timer1, 2500U);
        gdr_irq_nested_wait();
        gdr_irq_timer_stop(gdr_timer1);
        gdr_irq_done = 1;
        break;
    case GDR_IRQ_SCENARIO_ALIGN:
        gdr_irq_align_leaf();
        gdr_irq_done = 1;
        break;
    case GDR_IRQ_SCENARIO_FP: {
        volatile float complete = 1.5f;
        complete += 2.5f;
        gdr_irq_fp_sink = complete;
        gdr_irq_fp_leaf();
        gdr_irq_done = 1;
        break;
    }
    default:
        break;
    }
}

void Timer0_Handler(void)
{
#if defined(GDR_FIXTURE_SCHED_TESTS) && (GDR_FIXTURE_SCHED_TESTS == 1)
    if (gdr_sched_gate == 3U) {
        gdr_sched_timer0_begin();
        for (volatile uint32_t spin = 0;
             !gdr_sched_timer1_done && spin < 2000000U; spin++) {
        }
        gdr_sched_timer0_end();
        return;
    }
#endif
    gdr_irq0_body();
    __asm__ volatile("" ::: "memory");
}

__attribute__((noinline, used)) static void gdr_irq_wait(void)
{
    while (!gdr_irq_done) {
        __asm__ volatile("" ::: "memory");
    }
}

/* Deliberately leave SP 4-byte, but not 8-byte, aligned while the IRQ is
 * taken; xPSR[9] then proves the hardware padding branch. */
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

__attribute__((noinline, used)) void gdr_irq_cycle_done(void)
{
    __asm__ volatile("" ::: "memory");
}

static void gdr_irq_task(void *argument)
{
    (void)argument;
    gdr_irq_hw_init();
    for (;;) {
        if (gdr_irq_gate == 0U) {
            vTaskPrioritySet(NULL, 2);
            gdr_irq_cycle_done();
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        vTaskPrioritySet(NULL, 5);
        gdr_irq_fpca_write(0);
        gdr_irq_scenario = GDR_IRQ_SCENARIO_SINGLE;
        gdr_irq_done = 0;
        gdr_irq_timer_arm(gdr_timer0, 125000U);
        gdr_irq_wait();
        vTaskDelay(pdMS_TO_TICKS(100));
        if (gdr_irq_gate == 0U) {
            continue;
        }

        gdr_irq_fpca_write(0);
        gdr_irq_scenario = GDR_IRQ_SCENARIO_NESTED;
        gdr_irq_done = 0;
        gdr_irq_nested_seen = 0;
        gdr_irq_timer_arm(gdr_timer0, 125000U);
        gdr_irq_wait();
        vTaskDelay(pdMS_TO_TICKS(100));
        if (gdr_irq_gate == 0U) {
            continue;
        }

        gdr_irq_fpca_write(0);
        gdr_irq_scenario = GDR_IRQ_SCENARIO_ALIGN;
        gdr_irq_done = 0;
        gdr_irq_timer_arm(gdr_timer0, 125000U);
        gdr_irq_misaligned_wait();
        vTaskDelay(pdMS_TO_TICKS(100));
        if (gdr_irq_gate == 0U) {
            continue;
        }

        gdr_irq_fpca_write(0);
        gdr_irq_scenario = GDR_IRQ_SCENARIO_FP;
        gdr_irq_done = 0;
        gdr_irq_fpca_write(1);
        gdr_irq_timer_arm(gdr_timer0, 125000U);
        gdr_irq_wait();
        vTaskDelay(pdMS_TO_TICKS(100));
    }
}

void gdr_irq_fixture_start(void)
{
    configASSERT(xTaskCreate(gdr_irq_task, "gdr_irq", 192, NULL, 2, NULL) ==
                 pdPASS);
}

#endif
