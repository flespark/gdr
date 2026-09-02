/* QEMU mps2-an521 (SSE-200, dual Cortex-M33):
 * code in ssram-0, reached through the 0x10000000 secure alias (the
 * machine's init_svtor default; AN521 also boots CPU1 from INITSVTOR1 =
 * 0x10000000), data in ssram-1 at 0x28000000 (2 MiB).  Do not reuse the
 * AN385 RAM window 0x20000000: the SSE-200 only maps 4x32 KiB SRAM banks
 * there.
 *
 * Why the image lives at 0x10000000 and not 0x00000000: QEMU reads the
 * M-profile reset SP/PC from the vector table at init_svtor (0x10000000)
 * during the *first* reset, when the -kernel ROM blob has not yet been
 * copied into RAM and only rom_ptr_for_as() (which matches the blob's
 * registered address) can see the image.  With the image at 0x00000000 the
 * read at 0x10000000 falls through to empty RAM and the core boots with a
 * zero vector (INVSTATE fault).  The two addresses alias the same ssram-0,
 * so GDB symbols and the CPU1 INITSVTOR1 handshake stay consistent.
 */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x10000000, LENGTH = 4M
    RAM   (rwx) : ORIGIN = 0x28000000, LENGTH = 2M
}

SECTIONS
{
    .isr_vector :
    {
        KEEP(*(.isr_vector))
        . = ALIGN(4);
    } > FLASH

    .text :
    {
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
        _etext = .;
    } > FLASH

    _sidata = LOADADDR(.data);

    .data : ALIGN(4)
    {
        _sdata = .;
        *(.data*)
        . = ALIGN(4);
        _edata = .;
    } > RAM AT > FLASH

    .bss (NOLOAD) : ALIGN(4)
    {
        _sbss = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        _ebss = .;
    } > RAM

    /* heap_3 wraps newlib malloc; give it a small heap in RAM. */
    .heap (NOLOAD) :
    {
        PROVIDE(end = .);
        . = . + 32K;
        gdr_heap_limit = .;
    } > RAM
}
