/* Minimal SystemInit + software runtime counter for mps2-an521. */
#include <stdint.h>

static volatile uint32_t gdr_runtime_ticks;

void SystemInit(void)
{
    /* No board initialisation: QEMU powers up the core ready to run.  The
     * cross-core MHU doorbell interrupt is intentionally not wired up (see
     * cpu1_start.c), so there is nothing to arm here. */
}

void gdr_runtime_timer_init(void)
{
    gdr_runtime_ticks = 0;
}

void gdr_runtime_timer_tick(void)
{
    ++gdr_runtime_ticks;
}

uint32_t gdr_runtime_timer_value(void)
{
    return gdr_runtime_ticks;
}
