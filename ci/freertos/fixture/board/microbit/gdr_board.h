/* BBC micro:bit (Cortex-M0) board glue for the GDR FreeRTOS fixture. */
#ifndef GDR_BOARD_H
#define GDR_BOARD_H

#include <stdint.h>

/* The Cortex-M0 port has no MPU branch and no CLZ-based priority selection. */
#ifndef configENABLE_MPU
#define configENABLE_MPU 0
#endif
#ifndef configUSE_PORT_OPTIMISED_TASK_SELECTION
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#endif
#define GDR_PORT_PROVIDES_SYSTICK_HANDLER 1
#define GDR_BOARD_HAS_IRQ_FIXTURE 1

/* Deterministic HardFault fixture lives in board/microbit/fault.c. */
#define GDR_BOARD_HAS_FAULT_FIXTURE 1

#ifndef configCPU_CLOCK_HZ
#define configCPU_CLOCK_HZ ((unsigned long)16000000)
#endif

/* Reason: V10.4/10.5 ARM_CM3 port.c asserts
 * (portMAX_PRIGROUP_BITS - ulMaxPRIGROUPValue) == configPRIO_BITS by probing
 * the NVIC priority registers. QEMU's micro:bit Cortex-M0 model implements all 8
 * priority bits (writing 0xFF reads back 0xFF), so the probe counts 8 shifts
 * and configPRIO_BITS must be 8. The original 3 made every V10 kernel-direct
 * build die in gdr_fixture_assert_failed inside xPortStartScheduler; V11.1.0
 * only tolerated a wrong value because it dropped the exact-width assert.
 * With 8-bit priorities the lowest interrupt is number 255, so
 * configLIBRARY_LOWEST_INTERRUPT_PRIORITY is 255 here (the common header's
 * 15 assumes a 4-bit NVIC). */
#ifndef configPRIO_BITS
#define configPRIO_BITS 8
#endif
#ifndef configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY 255
#endif
#ifndef configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY
/* Reason: V11.1.0 port.c asserts (configMAX_SYSCALL_INTERRUPT_PRIORITY & 1)
 * == 0 when the NVIC implements 8 bits (the sub-priority confusion guard),
 * so the raw priority number must be even. With configPRIO_BITS 8 the
 * shifted value equals the raw number; 4 keeps 0-3 masked in critical
 * sections while SysTick/PendSV at 255 stay unmasked. */
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 4
#endif

#endif
