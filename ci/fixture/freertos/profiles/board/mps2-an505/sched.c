/* Deterministic scheduler-transition fixture for the FreeRTOS CM33 port.
 *
 * The gate lets the test harness choose one scheduler path at a time:
 * cooperative PendSV, tick preemption, or a nested timer IRQ whose inner
 * handler notifies a higher-priority task.
 */
#include "FreeRTOS.h"
#include "task.h"

#if defined(GDR_FIXTURE_SCHED_TESTS) && (GDR_FIXTURE_SCHED_TESTS == 1)

#include <stdint.h>

#define GDR_SCHED_TIMER0_BASE 0x50000000UL
#define GDR_SCHED_TIMER1_BASE 0x50001000UL
#define GDR_SCHED_TIMER_ENABLE 0x1U
#define GDR_SCHED_TIMER_IRQEN 0x8U

#define GDR_SCHED_NVIC_ISER (*(volatile uint32_t *)0xE000E100UL)
#define GDR_SCHED_NVIC_ICPR (*(volatile uint32_t *)0xE000E280UL)
#define GDR_SCHED_NVIC_IPR ((volatile uint8_t *)0xE000E400UL)

#define GDR_SCHED_IRQ_TIMER0 3U
#define GDR_SCHED_IRQ_TIMER1 4U

typedef struct {
    volatile uint32_t ctrl;
    volatile uint32_t value;
    volatile uint32_t reload;
    volatile uint32_t intstatus;
} gdr_sched_timer_t;

static gdr_sched_timer_t *const gdr_sched_timer0 =
    (gdr_sched_timer_t *)GDR_SCHED_TIMER0_BASE;
static gdr_sched_timer_t *const gdr_sched_timer1 =
    (gdr_sched_timer_t *)GDR_SCHED_TIMER1_BASE;

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

TaskHandle_t gdr_sched_first_handle;
TaskHandle_t gdr_sched_task_handle;
TaskHandle_t gdr_sched_tick_handle;
TaskHandle_t gdr_sched_irq_handle;

static void gdr_sched_timer_stop(gdr_sched_timer_t *timer)
{
    timer->ctrl = 0;
    timer->intstatus = 1;
}

static void gdr_sched_timer_arm(gdr_sched_timer_t *timer, uint32_t reload)
{
    timer->ctrl = 0;
    timer->intstatus = 1;
    timer->reload = reload;
    timer->value = reload;
    timer->ctrl = GDR_SCHED_TIMER_ENABLE | GDR_SCHED_TIMER_IRQEN;
}

static void gdr_sched_irq_hw_init(void)
{
    gdr_sched_timer_stop(gdr_sched_timer0);
    gdr_sched_timer_stop(gdr_sched_timer1);
    GDR_SCHED_NVIC_ICPR =
        (1U << GDR_SCHED_IRQ_TIMER0) | (1U << GDR_SCHED_IRQ_TIMER1);
    GDR_SCHED_NVIC_IPR[GDR_SCHED_IRQ_TIMER0] = 0xFFU;
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
    gdr_sched_timer_stop(gdr_sched_timer1);
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(gdr_sched_irq_handle, &woken);
    gdr_sched_irq_requested = 1;
    gdr_sched_timer1_done = 1;
    portYIELD_FROM_ISR(woken);
}

void gdr_sched_timer0_begin(void)
{
    gdr_sched_timer_stop(gdr_sched_timer0);
    gdr_sched_timer1_done = 0;
    gdr_sched_timer_arm(gdr_sched_timer1, 2000U);
}

void gdr_sched_timer0_end(void)
{
    gdr_sched_timer_stop(gdr_sched_timer1);
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
            gdr_sched_timer_arm(gdr_sched_timer0, 4000U);
            for (volatile uint32_t spin = 0;
                 spin < 20000000U && !gdr_sched_irq_requested; spin++) {
            }
            gdr_sched_timer_stop(gdr_sched_timer0);
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
