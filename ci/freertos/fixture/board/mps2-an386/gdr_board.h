/* ARM MPS2 AN386 (Cortex-M4) board glue for the GDR FreeRTOS fixture. */
#ifndef GDR_BOARD_H
#define GDR_BOARD_H

#include <stdint.h>

/* MPS2 AN386 runs the M4 test chip at 25 MHz, like the AN385 M3 board. */
#ifndef configCPU_CLOCK_HZ
#define configCPU_CLOCK_HZ ((unsigned long)25000000)
#endif

/* Reason: the CM4F port probes the NVIC priority registers; QEMU's
 * cortex-m4 model implements all 8 priority bits (writing 0xFF reads back
 * 0xFF), so configPRIO_BITS must be 8 and the lowest interrupt is 255. */
#ifndef configPRIO_BITS
#define configPRIO_BITS 8
#endif
#ifndef configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY 255
#endif
#ifndef configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 4
#endif

/* The AN386 board carries the same CMSDK timer IRQ fixture as AN385. */
#define GDR_BOARD_HAS_IRQ_FIXTURE 1

#endif
