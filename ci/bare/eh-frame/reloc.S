    .option norelax
    .section .text.start, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    la sp, stack_top
    call reloc_root
1:  j 1b
    .size _start, . - _start

    .text
    .globl text_base
text_base:

    .globl reloc_root
    .type reloc_root, @function
reloc_root:
    call reloc_middle
    nop
.Lreloc_root_end:
    .size reloc_root, . - reloc_root

    .globl reloc_middle
    .type reloc_middle, @function
reloc_middle:
    addi sp, sp, -16
.Lreloc_middle_1:
    sd ra, 8(sp)
.Lreloc_middle_2:
    call reloc_funcrel
    ld ra, 8(sp)
    addi sp, sp, 16
    ret
.Lreloc_middle_end:
    .size reloc_middle, . - reloc_middle

    .globl reloc_funcrel
    .type reloc_funcrel, @function
reloc_funcrel:
    addi sp, sp, -16
.Lreloc_funcrel_1:
    sd ra, 8(sp)
.Lreloc_funcrel_2:
    call reloc_indirect
    ld ra, 8(sp)
    addi sp, sp, 16
    ret
.Lreloc_funcrel_end:
    .size reloc_funcrel, . - reloc_funcrel

    .globl reloc_indirect
    .type reloc_indirect, @function
reloc_indirect:
    addi sp, sp, -16
.Lreloc_indirect_1:
    sd ra, 8(sp)
.Lreloc_indirect_2:
    .globl reloc_checkpoint
reloc_checkpoint:
1:  j 1b
.Lreloc_indirect_end:
    .size reloc_indirect, . - reloc_indirect

    .section .data, "aw", @progbits
    .balign 8
    .globl data_base
data_base:
    .globl indirect_slot
indirect_slot:
    .8byte reloc_indirect

    /* Emitted under .eh_frame_enc so GNU ld resolves cross-section subtractions
     * cleanly before objcopy renames the section to .eh_frame. */
    .section .eh_frame_enc, "a", @progbits
cie_textrel:
    .4byte .Lcie_textrel_end - . - 4
    .4byte 0
    .byte 1
    .asciz "zR"
    .uleb128 1
    .sleb128 -8
    .uleb128 1
    .uleb128 1
    .byte 0x2b
    .byte 0x0c, 2, 0
    .balign 4
.Lcie_textrel_end:
    .4byte .Lfde_textrel_end - . - 4
    .4byte . - cie_textrel
    .4byte reloc_root - text_base
    .4byte .Lreloc_root_end - reloc_root
    .uleb128 0
    .byte 0x07, 1
    .balign 4
.Lfde_textrel_end:

cie_datarel:
    .4byte .Lcie_datarel_end - . - 4
    .4byte 0
    .byte 1
    .asciz "zR"
    .uleb128 1
    .sleb128 -8
    .uleb128 1
    .uleb128 1
    .byte 0x3b
    .byte 0x0c, 2, 0
    .balign 4
.Lcie_datarel_end:
    .4byte .Lfde_datarel_end - . - 4
    .4byte . - cie_datarel
    .4byte reloc_middle - data_base
    .4byte .Lreloc_middle_end - reloc_middle
    .uleb128 0
    .byte 0x40 + (.Lreloc_middle_1 - reloc_middle)
    .byte 0x0e, 16
    .byte 0x40 + (.Lreloc_middle_2 - .Lreloc_middle_1)
    .byte 0x81, 1
    .balign 4
.Lfde_datarel_end:

cie_funcrel:
    .4byte .Lcie_funcrel_end - . - 4
    .4byte 0
    .byte 1
    .asciz "zR"
    .uleb128 1
    .sleb128 -8
    .uleb128 1
    .uleb128 1
    .byte 0x4b
    .byte 0x0c, 2, 0
    .balign 4
.Lcie_funcrel_end:
    .4byte .Lfde_funcrel_end - . - 4
    .4byte . - cie_funcrel
    .4byte 0
    .4byte .Lreloc_funcrel_end - reloc_funcrel
    .uleb128 0
    .byte 0x40 + (.Lreloc_funcrel_1 - reloc_funcrel)
    .byte 0x0e, 16
    .byte 0x40 + (.Lreloc_funcrel_2 - .Lreloc_funcrel_1)
    .byte 0x81, 1
    .balign 4
.Lfde_funcrel_end:

cie_indirect:
    .4byte .Lcie_indirect_end - . - 4
    .4byte 0
    .byte 1
    .asciz "zR"
    .uleb128 1
    .sleb128 -8
    .uleb128 1
    .uleb128 1
    .byte 0x9b
    .byte 0x0c, 2, 0
    .balign 4
.Lcie_indirect_end:
    .4byte .Lfde_indirect_end - . - 4
    .4byte . - cie_indirect
    .4byte indirect_slot - .
    .4byte .Lreloc_indirect_end - reloc_indirect
    .uleb128 0
    .byte 0x40 + (.Lreloc_indirect_1 - reloc_indirect)
    .byte 0x0e, 16
    .byte 0x40 + (.Lreloc_indirect_2 - .Lreloc_indirect_1)
    .byte 0x81, 1
    .balign 4
.Lfde_indirect_end:
    .4byte 0
