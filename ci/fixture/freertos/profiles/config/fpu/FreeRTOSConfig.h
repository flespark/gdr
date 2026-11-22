/* GDR fixture variant: fpu — single-core Cortex-M33 running the ARM_CM4F
 * port with the FPU enabled, so a preempted FP-using task carries the
 * 0x68-byte extended hardware frame plus the S16-S31 software block.
 *
 * configNUMBER_OF_CORES stays 1: the secondary core parks in
 * cpu1_start.c and only the primary boots the scheduler.  configENABLE_FPU
 * is the selection evidence the GDR adapter probes (the CM4F port itself
 * keys FPU codegen off __VFP_FP__, which the fpu lane's hard-float flags
 * provide); the port's lazy stacking (ASPEN|LSPEN) preserves S16-S31
 * across every PendSV switch without further kernel cooperation.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configENABLE_FPU 1

#include "gdr_board.h"

#define configNUMBER_OF_CORES 1
#define configENABLE_MPU 0
#define configENABLE_TRUSTZONE 0
#define configRECORD_STACK_HIGH_ADDRESS 1
#define GDR_FIXTURE_IRQ_TESTS 1
#define GDR_FIXTURE_SCHED_TESTS 1
#define GDR_FIXTURE_FAULT_TESTS 1

#include "gdr_fixture_common.h"

#endif
