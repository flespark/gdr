/* Minimal SystemInit + software runtime counter for mps2-an385. */
#include <stdint.h>

static volatile uint32_t gdr_runtime_ticks;

void SystemInit(void)
{
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
