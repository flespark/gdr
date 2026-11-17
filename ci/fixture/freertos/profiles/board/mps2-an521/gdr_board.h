/* ARM MPS2 AN521 (dual Cortex-M33, SSE-200) board glue for the GDR fixture. */
#ifndef GDR_BOARD_H
#define GDR_BOARD_H

#include <stdint.h>

/* Reason: the AN521 platform runs the SSE-200 at its 20 MHz sysclk
 * (QEMU hw/arm/mps2-tz.c an521 class init).  This is *not* the AN385
 * board's 25 MHz: reusing that header would mis-tick every delay and the
 * fixture's 80 ms ready-marker timing would drift. */
#ifndef configCPU_CLOCK_HZ
#define configCPU_CLOCK_HZ ((unsigned long)20000000)
#endif

/* Reason: the CM33_NTZ port gets the number of NVIC priority bits not from
 * configPRIO_BITS but from a runtime probe of SHPR2 (vPortConfigureInterrupt
 * Priorities asserts the configured maximum fits the implemented width).
 * Like the AN385 Cortex-M3 model, QEMU's Cortex-M33 implements all 8
 * priority bits, so the same 8-bit-compatible values apply and the asserted
 * even max-syscall priority (sub-priority guard) holds. */
#ifndef configPRIO_BITS
#define configPRIO_BITS 8
#endif
#ifndef configLIBRARY_LOWEST_INTERRUPT_PRIORITY
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY 255
#endif
#ifndef configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 4
#endif

/* Reason: the ARM_CM33_NTZ port defines SysTick_Handler itself in port.c
 * (there is no xPortSysTickHandler on this port), so the shared fixture's
 * main.c must not also emit a strong SysTick_Handler -- that would be a
 * duplicate-symbol link error.  gdr_fixture_common.h's
 * vPortSVCHandler->SVC_Handler alias is likewise a no-op here, because the
 * CM33 port names its handlers SVC_Handler/PendSV_Handler natively. */
#define GDR_PORT_PROVIDES_SYSTICK_HANDLER 1

#endif
