ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 2M
}

PHDRS
{
    text PT_LOAD FLAGS(7);
    eh_frame PT_GNU_EH_FRAME FLAGS(4);
}

SECTIONS
{
    .text : { KEEP(*(.text.start)) *(.text*) } > RAM :text
    .rodata : ALIGN(8) { *(.rodata*) } > RAM :text
    .data : ALIGN(8) { *(.data*) } > RAM :text
    .eh_frame : ALIGN(4) { KEEP(*(.eh_frame)) } > RAM :text
    .eh_frame_hdr : ALIGN(4) { KEEP(*(.eh_frame_hdr)) } > RAM :text :eh_frame
    .stack (NOLOAD) : ALIGN(16)
    {
        stack_low = .;
        . += 4096;
        stack_top = .;
    } > RAM :NONE
}
