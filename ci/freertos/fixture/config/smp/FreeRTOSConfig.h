/* GDR fixture variant: smp — true dual-core on QEMU mps2-an521.
 *
 * ARMv8-M SMP was introduced in FreeRTOS V11.3.1 (History.txt); the
 * CM33_NTZ non-secure GCC port is the only mainline port with
 * portVALIDATED_FOR_SMP == 1, and only a no-TrustZone, no-MPU build is
 * validated (portmacrocommon.h).  Missing any knob below is a compile-time
 * #error on this port, not a runtime failure.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"
#include "gdr_fixture_common.h"

/* The common header defaults configUSE_PORT_OPTIMISED_TASK_SELECTION to 1,
 * which SMP rejects; and it never defines configUSE_PASSIVE_IDLE_HOOK,
 * which SMP requires explicitly.  Both are overridden after the include. */
#undef configUSE_PORT_OPTIMISED_TASK_SELECTION
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#define configUSE_PASSIVE_IDLE_HOOK 0

/* SMP requires these (portmacrocommon.h:61-73 and FreeRTOS.h). */
#define configNUMBER_OF_CORES 2
#define configENABLE_TRUSTZONE 0
#define configENABLE_MPU 0
#define configENABLE_FPU 0
#define configRUN_FREERTOS_SECURE_ONLY 1
#define configUSE_CORE_AFFINITY 1
/* Reason: the kernel creates the timer daemon task itself with this affinity
 * (timers.c, default tskNO_AFFINITY).  Fixture main.c pins every ground-truth
 * waiter to core 0 so no shared assertion ever needs a cross-core yield, and
 * this lane's ready marker waits out the daemon's bootstrap burst -- but an
 * unpinned daemon can land on core 1 and be caught mid-update at snapshot,
 * which makes the timer list reads racy and breaks the SMP CPU-column row
 * parser (space in "Tmr Svc").  Pinning it to core 0 restores the
 * single-core scheduling semantics this fixture's assertions rely on. */
#define configTIMER_SERVICE_TASK_CORE_AFFINITY 0x1U
#define configUSE_TASK_PREEMPTION_DISABLE 1
#define configRUN_MULTIPLE_PRIORITIES 1
#define configCORE_ID_REGISTER 0x5001F000U
#define configWAKE_SECONDARY_CORES ConfigWakeSecondaryCores

/* The CPU identity register is byte-addressed by the port
 * (*(volatile uint8_t *)configCORE_ID_REGISTER, port.c) and must be a
 * compile-time constant for the inline asm "i" constraint.
 * 0x5001F000 reads 0 on CPU0 and 1 on CPU1 (SSE-200 CPU identity block,
 * confirmed by a QEMU spike).  Do not use the SCB CPUID (0xE000ED00):
 * both cores report 0x410fd213 there, so portGET_CORE_ID() would never
 * distinguish them and every core-affinity decision would silently break.
 */

#endif
