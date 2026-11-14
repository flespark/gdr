/* GDR fixture variant: fault.
 *
 * Keeps the full variant's DWARF knobs and adds the deterministic synchronous
 * fault fixture (precise UsageFault escalation on M3, unclassified HardFault
 * on M0). The IRQ and scheduler fixtures are absent so no timer handler can
 * nest inside the fault entry while GDR holds the halt.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configRECORD_STACK_HIGH_ADDRESS 1
#define INCLUDE_uxTaskGetStackHighWaterMark 1
#define INCLUDE_eTaskGetState 1
#define configGENERATE_RUN_TIME_STATS 1
#define configUSE_TICK_HOOK 1
#define configUSE_QUEUE_SETS 1
#define configNUM_THREAD_LOCAL_STORAGE_POINTERS 2
#define configCHECK_FOR_STACK_OVERFLOW 2

#define GDR_FIXTURE_FAULT_TESTS 1

#define portCONFIGURE_TIMER_FOR_RUN_TIME_STATS() gdr_runtime_timer_init()
#define portGET_RUN_TIME_COUNTER_VALUE() gdr_runtime_timer_value()

void gdr_runtime_timer_init(void);
uint32_t gdr_runtime_timer_value(void);

#include "gdr_fixture_common.h"

#endif
