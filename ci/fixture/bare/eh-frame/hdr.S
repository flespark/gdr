    .option norelax
    .section .text.start, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    la sp, stack_top
    call hdr_root
1:  j 1b
    .size _start, . - _start

    .text
.macro PAD_FN n
    .globl hdr_pad_\n
    .type hdr_pad_\n, @function
hdr_pad_\n:
    ret
.Lhdr_pad_end_\n:
    .size hdr_pad_\n, . - hdr_pad_\n
.endm

.irp idx, 00,01,02,03,04,05,06,07,08,09,10,11,12,13,14,15
    PAD_FN \idx
.endr

    .globl hdr_root
    .type hdr_root, @function
hdr_root:
    call hdr_middle
    nop
.Lhdr_root_end:
    .size hdr_root, . - hdr_root

    .globl hdr_middle
    .type hdr_middle, @function
hdr_middle:
    addi sp, sp, -16
.Lhdr_middle_1:
    sd ra, 8(sp)
.Lhdr_middle_2:
    call hdr_leaf
    ld ra, 8(sp)
    addi sp, sp, 16
    ret
.Lhdr_middle_end:
    .size hdr_middle, . - hdr_middle

    .globl hdr_leaf
    .type hdr_leaf, @function
hdr_leaf:
    addi sp, sp, -16
.Lhdr_leaf_1:
    sd ra, 8(sp)
.Lhdr_leaf_2:
    call hdr_checkpoint
    ld ra, 8(sp)
    addi sp, sp, 16
    ret
.Lhdr_leaf_end:
    .size hdr_leaf, . - hdr_leaf

    .globl hdr_checkpoint
    .type hdr_checkpoint, @function
hdr_checkpoint:
1:  j 1b
.Lhdr_checkpoint_end:
    .size hdr_checkpoint, . - hdr_checkpoint

.irp idx, 16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
    PAD_FN \idx
.endr

    .section .eh_frame, "a", @progbits
eh_base:
cie0:
    .4byte .Lcie0_end - . - 4
    .4byte 0
    .byte 1
    .asciz "zR"
    .uleb128 1
    .sleb128 -8
    .uleb128 1
    .uleb128 1
    .byte 0x1b
    .byte 0x0c, 2, 0
    .balign 4
.Lcie0_end:

.macro PAD_FDE n
fde_pad_\n:
    .4byte .Lfde_pad_end_\n - . - 4
    .4byte . - cie0
    .4byte hdr_pad_\n - .
    .4byte .Lhdr_pad_end_\n - hdr_pad_\n
    .uleb128 0
    .byte 0x07, 1
    .balign 4
.Lfde_pad_end_\n:
.endm

.irp idx, 00,01,02,03,04,05,06,07,08,09,10,11,12,13,14,15
    PAD_FDE \idx
.endr

fde_root:
    .4byte .Lfde_root_end - . - 4
    .4byte . - cie0
    .4byte hdr_root - .
    .4byte .Lhdr_root_end - hdr_root
    .uleb128 0
    .byte 0x07, 1
    .balign 4
.Lfde_root_end:

fde_middle:
    .4byte .Lfde_middle_end - . - 4
    .4byte . - cie0
    .4byte hdr_middle - .
    .4byte .Lhdr_middle_end - hdr_middle
    .uleb128 0
    .byte 0x40 + (.Lhdr_middle_1 - hdr_middle)
    .byte 0x0e, 16
    .byte 0x40 + (.Lhdr_middle_2 - .Lhdr_middle_1)
    .byte 0x81, 1
    .balign 4
.Lfde_middle_end:

fde_leaf:
    .4byte .Lfde_leaf_end - . - 4
    .4byte . - cie0
    .4byte hdr_leaf - .
    .4byte .Lhdr_leaf_end - hdr_leaf
    .uleb128 0
    .byte 0x40 + (.Lhdr_leaf_1 - hdr_leaf)
    .byte 0x0e, 16
    .byte 0x40 + (.Lhdr_leaf_2 - .Lhdr_leaf_1)
    .byte 0x81, 1
    .balign 4
.Lfde_leaf_end:

fde_checkpoint:
    .4byte .Lfde_checkpoint_end - . - 4
    .4byte . - cie0
    .4byte hdr_checkpoint - .
    .4byte .Lhdr_checkpoint_end - hdr_checkpoint
    .uleb128 0
    .byte 0x08, 1
    .balign 4
.Lfde_checkpoint_end:

.irp idx, 16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
    PAD_FDE \idx
.endr

    .4byte 0

    .section .eh_frame_hdr, "a", @progbits
eh_hdr_base:
    .byte 1, 0x1b, 0x03, 0x3b
    .4byte eh_base - .
    .4byte 36

.irp idx, 00,01,02,03,04,05,06,07,08,09,10,11,12,13,14,15
    .4byte hdr_pad_\idx - eh_hdr_base
    .4byte fde_pad_\idx - eh_hdr_base
.endr
    .4byte hdr_root - eh_hdr_base
    .4byte fde_root - eh_hdr_base
    .4byte hdr_middle - eh_hdr_base
    .4byte fde_middle - eh_hdr_base
    .4byte hdr_leaf - eh_hdr_base
    .4byte fde_leaf - eh_hdr_base
    .4byte hdr_checkpoint - eh_hdr_base
    .4byte fde_checkpoint - eh_hdr_base
.irp idx, 16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31
    .4byte hdr_pad_\idx - eh_hdr_base
    .4byte fde_pad_\idx - eh_hdr_base
.endr
