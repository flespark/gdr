/* Minimal Cortex-M portmacro for the static snapshot TU. */
#ifndef PORTMACRO_H
#define PORTMACRO_H

#include <stdint.h>

#define portCHAR char
#define portFLOAT float
#define portDOUBLE double
#define portLONG long
#define portSHORT short
#define portSTACK_TYPE uint32_t
#define portBASE_TYPE long

typedef portSTACK_TYPE StackType_t;
typedef long BaseType_t;
typedef unsigned long UBaseType_t;
typedef uint32_t TickType_t;

#define portMAX_DELAY ((TickType_t)0xffffffffUL)
#define portSTACK_GROWTH (-1)
#define portTICK_PERIOD_MS ((TickType_t)1000 / configTICK_RATE_HZ)
#define portBYTE_ALIGNMENT 8
#define portTICK_TYPE_IS_ATOMIC 1

#define portDISABLE_INTERRUPTS()
#define portENABLE_INTERRUPTS()
#define portENTER_CRITICAL()
#define portEXIT_CRITICAL()
/* Reason: the snapshot never runs the scheduler; these satisfy the V11 SMP
 * compile-time contract while preserving the real ABI types in DWARF. */
#define portSET_INTERRUPT_MASK() ((UBaseType_t)0)
#define portCLEAR_INTERRUPT_MASK(value) ((void)(value))
#define portGET_TASK_LOCK(core_id) ((void)(core_id))
#define portRELEASE_TASK_LOCK(core_id) ((void)(core_id))
#define portGET_ISR_LOCK(core_id) ((void)(core_id))
#define portRELEASE_ISR_LOCK(core_id) ((void)(core_id))
#define portENTER_CRITICAL_FROM_ISR() ((UBaseType_t)0)
#define portEXIT_CRITICAL_FROM_ISR(value) ((void)(value))
#define portYIELD()
#define portNOP()
#define portINLINE inline
#define portFORCE_INLINE inline
#define portDONT_DISCARD
#define PRIVILEGED_FUNCTION
#define PRIVILEGED_DATA
#define portUSING_MPU_WRAPPERS 0

#endif
