/* Secondary-core (CPU1) bootstrap for the QEMU mps2-an521 dual Cortex-M33.
 *
 * The SSE-200 keeps CPU1 in a wait-for-reset state until the primary core
 * writes INITSVTOR1 (warm-reset vector base) and clears CPUWAIT bit 1
 * (arm_set_cpu_on_and_reset in QEMU).  Both steps are board glue: they must
 * not live in the shared main.c, which every lane compiles.
 *
 * After the reset, CPU1 executes the same vector table as CPU0 (INITSVTOR1
 * default 0x10000000 aliases the 0x00000000 image) and runs the official
 * secondary-core flow from portable/ARMv8M/non_secure/ReadMe.txt:
 * wait for ucPrimaryCoreInitDoneFlag, program per-core interrupt
 * priorities, report ready via ucSecondaryCoresReadyFlags[core-1], then
 * `svc 102` (portSVC_START_SCHEDULER) to enter vRestoreContextOfFirstTask.
 *
 * This file is the CPU1 bootstrap only: on single-core configs it compiles
 * to nothing (the SMP handshake globals do not exist there), so it can be
 * part of the board source list unconditionally.
 *
 * Cross-core preemption: the port's vInterruptCore is a weak no-op and this
 * board does not override it.  QEMU's SSE-200 does model two MHU doorbells,
 * but they sit in the shared container at 0x40003000/0x40004000 (the
 * 0x5000_0000 alias maps the per-CPU container, so the secure alias is not a
 * usable doorbell address), and delivering a cross-core interrupt into this
 * configRUN_FREERTOS_SECURE_ONLY 1 build was not validated.  A cross-core
 * yield request is therefore dropped and a blocked task's xTaskRunState can
 * park on taskTASK_SCHEDULED_TO_YIELD until its own tick.  main.c avoids
 * depending on this by pinning every ground-truth waiter task to core 0
 * (configNUMBER_OF_CORES > 1), so no shared assertion ever needs a cross-core
 * yield to be delivered.  This is a known fixture limitation (see
 * ci/freertos/README.md), not a GDR defect.
 */
#include <stdint.h>

#include "FreeRTOSConfig.h"

#if (configNUMBER_OF_CORES > 1)

/* SSE-200 SysCtrl block, secure alias.  (configCORE_ID_REGISTER is the CPU
 * identity block at 0x5001F000; this is a different block.) */
#define GDR_SYSCTL_BASE 0x50021000U
#define GDR_SYSCTL_INITSVTOR1 (GDR_SYSCTL_BASE + 0x114U)
#define GDR_SYSCTL_CPUWAIT (GDR_SYSCTL_BASE + 0x118U)

/* CPU identity block of the executing core: 0 on CPU0, 1 on CPU1. */
#define GDR_CORE_ID_REG 0x5001F000U

/* Kernel-provided SMP handshake state (non-static in the SMP port build).
 * The ready-flags array is configNUMBER_OF_CORES - 1 = 1 on this 2-core
 * board. */
extern volatile uint8_t ucPrimaryCoreInitDoneFlag;
extern volatile uint8_t ucSecondaryCoresReadyFlags[1];

/* port.c exports this; no public header declares it for the GCC port. */
extern void vPortConfigureInterruptPriorities(void);

/* portmacrocommon.h: SVC immediate that starts the first task on this core.
 * Must stay in sync with the port's own definition. */
#define GDR_PORT_SVC_START_SCHEDULER 102U

/* CPU1 private stack (startup.s moves SP here before branching into the C
 * entry, so both cores never share one growing stack region).  The label
 * sits *after* the reserved area -- "top of stack".  The byte count is a
 * literal matching GDR_CPU1_STACK_WORDS * sizeof(uint32_t). */
#define GDR_CPU1_STACK_WORDS 512U
__asm__(
    ".section .bss.gdr_cpu1_stack,\"aw\",%nobits\n"
    ".balign 8\n"
    ".space 2048\n"
    ".global gdr_cpu1_stack_top\n"
    "gdr_cpu1_stack_top:\n"
    ".previous\n");

/* Called from startup.s on CPU1 (SP already switched).  Enter the FreeRTOS
 * secondary-core flow; never returns. */
__attribute__((noreturn))
void gdr_secondary_core_entry(void)
{
    /* Step 1 of the secondary flow: spin until the primary finished shared
     * initialisation (xPortStartScheduler sets the flag before calling
     * configWAKE_SECONDARY_CORES). */
    while (ucPrimaryCoreInitDoneFlag == 0U) {
    }

    /* Step 2: per-core interrupt priorities (lowest-priority PendSV/SysTick,
     * highest SVCall). */
    vPortConfigureInterruptPriorities();

    /* Step 3: signal the primary core that this secondary is online and
     * ready (pdTRUE == 1).  The inter-core doorbell stays unconfigured
     * (vInterruptCore is a weak no-op in the port), which only delays
     * cross-core preemption requests -- never the boot itself. */
    ucSecondaryCoresReadyFlags[0] = 1U;

    /* Step 4: enter the scheduler via SVC #102 -> vRestoreContextOfFirstTask.
     * The immediate must be a compile-time constant ("i" constraint). */
    __asm__ volatile("svc %0" ::"i"(GDR_PORT_SVC_START_SCHEDULER) : "memory");
    for (;;) {
    }
}

/* The wake callback the kernel invokes from xPortStartScheduler (port.c):
 * write INITSVTOR1 first -- clearing CPUWAIT triggers CPU1's *warm reset*,
 * which picks up the vector base that is already programmed -- then release
 * the CPU1 wait bit.  Returns pdTRUE once released; the primary core spins
 * on ucSecondaryCoresReadyFlags afterwards, so this function only releases,
 * it never waits.  The name is fixed by the config/smp variant's
 * configWAKE_SECONDARY_CORES macro; the return type matches port.c's extern
 * (BaseType_t is long on ARM). */
long ConfigWakeSecondaryCores(void)
{
    volatile uint32_t *const initsvtor1 =
        (volatile uint32_t *)GDR_SYSCTL_INITSVTOR1;
    volatile uint32_t *const cpuwait = (volatile uint32_t *)GDR_SYSCTL_CPUWAIT;

    /* INITSVTOR1 reset value is already 0x10000000 (the image alias), so the
     * write is idempotent; keep it explicit and ordered before CPUWAIT. */
    *initsvtor1 = 0x10000000U;
    *cpuwait = 0U;
    return 1; /* pdTRUE */
}

#else /* configNUMBER_OF_CORES == 1 */

/* Single-core build: CPU1 is never released (no configWAKE_SECONDARY_CORES
 * call), so no kernel SMP symbols exist.  startup.s still branches to this
 * entry symbol, so it must resolve; a parked loop is correct.  The private
 * stack label lives in the same #if as the entry so the unconditional
 * `ldr r0, =gdr_cpu1_stack_top` in startup.s also resolves. */
__asm__(
    ".section .bss.gdr_cpu1_stack,\"aw\",%nobits\n"
    ".balign 8\n"
    ".space 2048\n"
    ".global gdr_cpu1_stack_top\n"
    "gdr_cpu1_stack_top:\n"
    ".previous\n");
__attribute__((noreturn))
void gdr_secondary_core_entry(void)
{
    for (;;) {
    }
}

#endif /* configNUMBER_OF_CORES > 1 */
