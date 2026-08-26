/* Bare Cortex-M33 snapshot: flash + RAM, no startup. All data is in .data. */
ENTRY(_start)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 256K
}

SECTIONS
{
    .text : { *(.text*) *(.rodata*) } > FLASH
    .data : ALIGN(4)
    {
        KEEP(*(.data*))
        KEEP(*(.gdr_snapshot*))
    } > RAM AT > FLASH
    .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM
}
