/* Software runtime-stats source shared by every QEMU board.
 *
 * The runtime-counter triple (gdr_runtime_timer_init/tick/value) is the
 * same on every board; keeping it in fixture/common avoids four copies
 * that can drift apart.  QEMU has no cycle counter to hook, so the count
 * advances from main.c's SysTick handler when the config variant uses
 * runtime statistics. */
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
