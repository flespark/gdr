    .option norelax
    .cfi_sections .eh_frame
    .section .text.start, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    la sp, stack_top
    call cfi_root
1:  j 1b
    .size _start, . - _start

    .text
    .globl cfi_root
    .type cfi_root, @function
cfi_root:
    .cfi_startproc simple
    .cfi_def_cfa sp, 0
    .cfi_undefined ra
    call cfi_middle
    nop
    .cfi_endproc
    .size cfi_root, . - cfi_root

    .globl cfi_middle
    .type cfi_middle, @function
cfi_middle:
    .cfi_startproc simple
    .cfi_def_cfa sp, 0
    addi sp, sp, -16
    .cfi_def_cfa_offset 16
    sd ra, 8(sp)
    .cfi_escape 0x05, 1, 2
    call cfi_leaf
    ld ra, 8(sp)
    addi sp, sp, 16
    ret
    .cfi_endproc
    .size cfi_middle, . - cfi_middle

    .globl cfi_leaf
    .type cfi_leaf, @function
cfi_leaf:
    .cfi_startproc simple
    .cfi_return_column 128
    .cfi_def_cfa sp, 0
    addi sp, sp, -16
    .cfi_def_cfa_offset 16
    sd ra, 8(sp)
    .cfi_offset 128, -8
    call cfi_checkpoint
    ld ra, 8(sp)
    addi sp, sp, 16
    ret
    .cfi_endproc
    .size cfi_leaf, . - cfi_leaf

    .globl cfi_checkpoint
    .type cfi_checkpoint, @function
cfi_checkpoint:
    .cfi_startproc simple
    .cfi_def_cfa sp, 0
    .cfi_same_value ra
1:  j 1b
    .cfi_endproc
    .size cfi_checkpoint, . - cfi_checkpoint

    .globl cfi_signal_entry
    .type cfi_signal_entry, @function
cfi_signal_entry:
    la sp, stack_top
    la t0, cfi_signal_resume
    j cfi_signal_checkpoint
    .size cfi_signal_entry, . - cfi_signal_entry

    .globl cfi_signal_checkpoint
    .type cfi_signal_checkpoint, @function
cfi_signal_checkpoint:
    .cfi_startproc simple
    .cfi_signal_frame
    .cfi_return_column 5
    .cfi_def_cfa sp, 0
    .cfi_same_value 5
1:  j 1b
    .cfi_endproc
    .size cfi_signal_checkpoint, . - cfi_signal_checkpoint

    .globl cfi_signal_resume
    .type cfi_signal_resume, @function
cfi_signal_resume:
    .cfi_startproc simple
    .cfi_def_cfa sp, 0
    .cfi_undefined ra
1:  j 1b
    .cfi_endproc
    .size cfi_signal_resume, . - cfi_signal_resume
