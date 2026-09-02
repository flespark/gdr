/* Cortex-M33 reset vector for the QEMU mps2-an521 machine (dual core).
 *
 * Both cores share this one vector table: the SSE-200 boots both from
 * INITSVTOR (0x00000000 default for CPU0, 0x10000000 for CPU1 -- the same
 * image aliased at +0x10000000).  FreeRTOS checks that vector slots 11/14
 * hold SVC_Handler/PendSV_Handler (configCHECK_HANDLER_INSTALLATION), and
 * the CM33_NTZ port provides strong definitions for SVC_Handler,
 * PendSV_Handler and SysTick_Handler in portasm.c/port.c, so the weak
 * aliases below are only a fallback for the non-RTOS exceptions.
 *
 * Reset_Handler branches on the SSE-200 CPU identity register
 * (0x5001F000: 0 on CPU0, 1 on CPU1).  Only CPU0 copies .data/zeroes .bss
 * and enters main(); CPU1 jumps into the FreeRTOS secondary-core bootstrap
 * in cpu1_start.c with a private stack.
 */
    .syntax unified
    .cpu cortex-m33
    .thumb

    .section .isr_vector, "a", %progbits
    .align 2
    .global g_pfnVectors
g_pfnVectors:
    .word _estack
    .word Reset_Handler
    .word NMI_Handler
    .word HardFault_Handler
    .word MemManage_Handler
    .word BusFault_Handler
    .word UsageFault_Handler
    .word 0
    .word 0
    .word 0
    .word 0
    .word SVC_Handler
    .word DebugMon_Handler
    .word 0
    .word PendSV_Handler
    .word SysTick_Handler
    /* External IRQs 0..7: no board IRQ is serviced (the cross-core MHU
     * doorbell interrupts are intentionally not wired; see cpu1_start.c). */
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0

    .text
    .thumb
    .thumb_func
    .global Reset_Handler
Reset_Handler:
    ldr r0, =_estack
    mov sp, r0
    /* SSE-200 CPU identity block (secure alias): 0 on CPU0, 1 on CPU1. */
    ldr r1, =0x5001F000
    ldrb r0, [r1]
    cmp r0, #0
    bne 1f
    ldr r0, =_sdata
    ldr r1, =_edata
    ldr r2, =_sidata
    b 3f
2:
    ldr r3, [r2], #4
    str r3, [r0], #4
3:
    cmp r0, r1
    bcc 2b
    ldr r0, =_sbss
    ldr r1, =_ebss
    movs r2, #0
    b 5f
4:
    str r2, [r0], #4
5:
    cmp r0, r1
    bcc 4b
    bl SystemInit
    bl main
6:
    b 6b
1:  /* Secondary core: switch to the private stack and enter FreeRTOS.
     * The mov sp, r0 is essential -- reset gives BOTH cores SP=_estack,
     * and running the secondary bootstrap on the shared _estack collides
     * with the primary's frames (observed: corrupted LR, jump to 0). */
    ldr r0, =gdr_cpu1_stack_top
    mov sp, r0
    ldr r0, =gdr_secondary_core_entry
    bx r0

    .thumb_func
    .weak Default_Handler
Default_Handler:
    b Default_Handler

    .macro weak_alias name
    .weak \name
    .thumb_set \name, Default_Handler
    .endm

    weak_alias NMI_Handler
    weak_alias HardFault_Handler
    weak_alias MemManage_Handler
    weak_alias BusFault_Handler
    weak_alias UsageFault_Handler
    weak_alias SVC_Handler
    weak_alias DebugMon_Handler
    weak_alias PendSV_Handler
    weak_alias SysTick_Handler
