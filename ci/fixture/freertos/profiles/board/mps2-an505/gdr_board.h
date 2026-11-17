/* ARM MPS2 AN505 (single Cortex-M33, SSE-200) board glue for the GDR fixture. */
#ifndef GDR_BOARD_H
#define GDR_BOARD_H

#include <stdint.h>

/* AN505 runs the SSE-200 at its 20 MHz sysclk (QEMU hw/arm/mps2-tz.c). */
#ifndef configCPU_CLOCK_HZ
#define configCPU_CLOCK_HZ ((unsigned long)20000000)
#endif

/* Cortex-M33 implements all 8 NVIC priority bits on QEMU. */
#ifndef configPRIO_BITS
#define configPRIO_BITS 8
#endif
#ifndef configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY 255
#endif
#ifndef configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 4
#endif

/* Single-core non-TrustZone, non-MPU, soft-float configuration. */
#ifndef configENABLE_TRUSTZONE
#define configENABLE_TRUSTZONE 0
#endif
#ifndef configENABLE_MPU
#define configENABLE_MPU 0
#endif
#ifndef configENABLE_FPU
#define configENABLE_FPU 0
#endif
#ifndef configRUN_FREERTOS_SECURE_ONLY
#define configRUN_FREERTOS_SECURE_ONLY 1
#endif

#define GDR_PORT_PROVIDES_SYSTICK_HANDLER 1
#define GDR_BOARD_HAS_IRQ_FIXTURE 1
#define GDR_BOARD_HAS_FAULT_FIXTURE 1
#define GDR_BOARD_HAS_SCHED_FIXTURE 1

#endif
