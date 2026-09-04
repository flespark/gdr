/* QEMU RISC-V virt: 128 MiB RAM at 0x80000000, no flash. */
ENTRY(_start)

_stack_top = ORIGIN(RAM) + LENGTH(RAM);

MEMORY
{
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 128M
}

SECTIONS
{
    .text :
    {
        KEEP(*(.text.startup))
        *(.text*)
        *(.rodata*)
        . = ALIGN(16);
        _etext = .;
    } > RAM

    .data : ALIGN(16)
    {
        _sdata = .;
        *(.data*)
        . = ALIGN(16);
        _edata = .;
    } > RAM

    .bss (NOLOAD) : ALIGN(16)
    {
        _sbss = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(16);
        _ebss = .;
    } > RAM

    /* heap_3 wraps newlib malloc; give it a small arena above the image. */
    .heap (NOLOAD) : ALIGN(16)
    {
        PROVIDE(end = .);
        . = . + 64K;
        gdr_heap_limit = .;
    } > RAM
}
