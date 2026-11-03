/* Cortex-M0 vectors for the QEMU micro:bit machine. */
    .syntax unified
    .cpu cortex-m0
    .thumb

    .section .isr_vector, "a", %progbits
    .align 2
    .global g_pfnVectors
g_pfnVectors:
    .word _estack
    .word Reset_Handler
    .word NMI_Handler
    .word HardFault_Handler
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0
    .word 0
    .word SVC_Handler
    .word 0
    .word 0
    .word PendSV_Handler
    .word SysTick_Handler
    .rept 8
    .word Default_Handler
    .endr
    .word Timer0_Handler
    .word Timer1_Handler
    .rept 22
    .word Default_Handler
    .endr
    .global __gdr_vector_table_end
__gdr_vector_table_end:

    .text
    .thumb
    .thumb_func
    .global Reset_Handler
Reset_Handler:
    ldr r0, =_estack
    mov sp, r0
    ldr r0, =_sdata
    ldr r1, =_edata
    ldr r2, =_sidata
    b 2f
1:
    ldr r3, [r2]
    str r3, [r0]
    adds r2, r2, #4
    adds r0, r0, #4
2:
    cmp r0, r1
    bcc 1b
    ldr r0, =_sbss
    ldr r1, =_ebss
    movs r2, #0
    b 4f
3:
    str r2, [r0]
    adds r0, r0, #4
4:
    cmp r0, r1
    bcc 3b
    bl SystemInit
    bl main
5:
    b 5b

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
    weak_alias SVC_Handler
    weak_alias PendSV_Handler
    weak_alias SysTick_Handler
    weak_alias Timer0_Handler
    weak_alias Timer1_Handler
