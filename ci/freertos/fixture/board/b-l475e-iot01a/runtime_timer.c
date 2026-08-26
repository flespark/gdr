/* Software runtime-stats source for the B-L475E-IOT01A QEMU fixture. */
#include <stdint.h>

static volatile uint32_t gdr_runtime_ticks;

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
