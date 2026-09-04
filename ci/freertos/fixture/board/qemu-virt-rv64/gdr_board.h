/* QEMU RISC-V virt (rv64) board glue for the GDR FreeRTOS fixture. */
#ifndef GDR_BOARD_H
#define GDR_BOARD_H

#include <stdint.h>

/* Reason: QEMU's virt machine drives the SiFive CLINT at exactly 10 MHz
 * (hw/riscv/virt.c TimebaseFrequency), so the FreeRTOS tick-rate
 * conversion (configCPU_CLOCK_HZ / configTICK_RATE_HZ = 10000 mtime ticks
 * per 1 kHz tick) is exact and the fixture's 80 ms ready-marker timing
 * holds. */
#ifndef configCPU_CLOCK_HZ
#define configCPU_CLOCK_HZ ((unsigned long)10000000)
#endif

/* Reason: port.c vPortSetupTimerInterrupt programs the CLINT from these
 * two macros and only #warnings when they are missing -- a fixture that
 * forgot them would boot with no tick at all, indistinguishable from a
 * crash.  QEMU virt places the CLINT at 0x2000000 (dumpdtb), SiFive
 * layout fixed: mtimecmp = base + 0x4000, mtime = base + 0xbff8. */
#ifndef configMTIME_BASE_ADDRESS
#define configMTIME_BASE_ADDRESS 0x2000000UL
#endif
#ifndef configMTIMECMP_BASE_ADDRESS
#define configMTIMECMP_BASE_ADDRESS 0x2004000UL
#endif

/* Reason: the RISC-V port selects the highest-priority ready task in
 * software (no CLZ-based port optimisation macro exists here), while the
 * shared common header defaults configUSE_PORT_OPTIMISED_TASK_SELECTION
 * to 1 -- the ARM-optimised taskSELECT_* macros would not compile. */
#ifndef configUSE_PORT_OPTIMISED_TASK_SELECTION
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#endif

/* Reason: the RISC-V port needs an interrupt stack; with this macro it
 * allocates one statically (port.c) instead of requiring the linker script
 * to define __freertos_irq_stack_top. */
#define configISR_STACK_SIZE_WORDS 512

/* The RISC-V port services the tick from its M-mode trap handler
 * (freertos_risc_v_trap_handler), so the shared fixture's ARM SysTick
 * wrapper must not be compiled (it would fail to link against
 * xPortSysTickHandler, an ARM-only symbol). */
#define GDR_PORT_PROVIDES_SYSTICK_HANDLER 1
/* main.c's gdr_semihosting_write0 is the ARM bkpt-0xab form; the RISC-V
 * board provides its own ebreak-sequenced implementation. */
#define GDR_BOARD_PROVIDES_SEMIHOSTING 1

#endif
