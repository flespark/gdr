ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 2M
}

SECTIONS
{
    .text : { KEEP(*(.text.start)) *(.text*) } > RAM
    .eh_frame : { KEEP(*(.eh_frame)) } > RAM
    .stack (NOLOAD) : ALIGN(16)
    {
        stack_low = .;
        . += 4096;
        stack_top = .;
    } > RAM
}
