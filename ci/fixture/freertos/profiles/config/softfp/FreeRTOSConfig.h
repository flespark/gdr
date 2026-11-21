/* GDR fixture variant: softfp — single-core Cortex-M4 running ARM_CM4F
 * under -mfloat-abi=softfp -mfpu=fpv4-sp-d16 (aapcs32 base ABI with VFP).
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configNUMBER_OF_CORES 1
#define configENABLE_FPU 1
#define configENABLE_MPU 0
#define configENABLE_TRUSTZONE 0
#define configRECORD_STACK_HIGH_ADDRESS 1
#define GDR_FIXTURE_IRQ_TESTS 1
#define GDR_FIXTURE_SCHED_TESTS 1
#define GDR_FIXTURE_FAULT_TESTS 1

#include "gdr_fixture_common.h"

#endif
