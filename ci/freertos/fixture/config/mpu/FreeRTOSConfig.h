/* GDR fixture variant: mpu.
 *
 * Single-core CM33 with configENABLE_MPU 1, so the ARM_CM33_NTZ port sets
 * portUSING_MPU_WRAPPERS and the kernel links against the MPU wrappers v2
 * (portable/Common/mpu_wrappers_v2.c).  Every xTaskCreate/xQueueGenericCreate
 * macro-expands to its MPU_ counterpart, which records the object handle in
 * xKernelObjectPool -- the mpu-pool discovery channel's first live fixture.
 * configNUMBER_OF_CORES must stay 1: SMP requires configENABLE_MPU == 0
 * (they are mutually exclusive by design).
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configENABLE_MPU 1
#define configUSE_MPU_WRAPPERS_V1 0
/* Reason: every MPU_* create records its handle in xKernelObjectPool; the
 * fixture creates 18 kernel objects + 16 tasks + daemon/idle tasks at
 * scheduler start (~37), so the pool must exceed that to leave empty slots
 * (the discovery channel's empty-slot skip is part of the assertions). */
#define configPROTECTED_KERNEL_OBJECT_POOL_SIZE 48
/* Reason: the wrappers v2 SVC gate swaps onto a dedicated system-call
 * stack; the port requires the size in words (portmacrocommon.h errors
 * when it is missing). */
#define configSYSTEM_CALL_STACK_SIZE 128
/* Reason: the portmacrocommon.h file for this port demands the three
 * configENABLE_* macros be defined explicitly. */
#define configENABLE_FPU 0
#define configENABLE_TRUSTZONE 0

#include "gdr_fixture_common.h"

#endif
