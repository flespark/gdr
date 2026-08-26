/* GDR fixture variant: full.
 *
 * Enables pxEndOfStack, runtime stats, queue sets, TLS pointers, and
 * method-2 stack overflow checking so those DWARF fields exist.
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

#define portCONFIGURE_TIMER_FOR_RUN_TIME_STATS() gdr_runtime_timer_init()
#define portGET_RUN_TIME_COUNTER_VALUE() gdr_runtime_timer_value()

void gdr_runtime_timer_init(void);
uint32_t gdr_runtime_timer_value(void);

#include "gdr_fixture_common.h"

#endif
