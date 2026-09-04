/* RISC-V (rv64) reset entry for the QEMU virt machine. */
    .section .text.startup, "ax", %progbits
    .globl _start
    .type _start, %function
_start:
    /* Machine trap handler must be installed before any interrupt can
     * fire; the port's context switch lives in freertos_risc_v_trap_handler
     * (portASM.S). */
    la      sp, _stack_top
    csrw    mstatus, zero
    la      t0, freertos_risc_v_trap_handler
    csrw    mtvec, t0
    call    main
1:  j       1b
    .size _start, . - _start
