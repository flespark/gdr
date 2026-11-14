/* QEMU mps2-an505 (SSE-200, Cortex-M33):
 * code in ssram-0 via the 0x10000000 secure alias (init_svtor default),
 * data in ssram-1 at 0x28000000 (2 MiB).
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
        . = ALIGN(32);
    } > FLASH

    .text :
    {
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
        _etext = .;
    } > FLASH

    .ARM.exidx :
    {
        __exidx_start = .;
        *(.ARM.exidx*)
        __exidx_end = .;
        . = ALIGN(4);
    } > FLASH

    .ARM.extab :
    {
        *(.ARM.extab*)
        . = ALIGN(4);
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

    /* Reserved main-stack (MSP / interrupt) region. */
    .irq_stack ORIGIN(RAM) + LENGTH(RAM) - 16K (NOLOAD) :
    {
        __gdr_irq_stack_low = .;
        . += 16K;
        __gdr_irq_stack_top = .;
    } > RAM
}

ASSERT(__gdr_irq_stack_top == ORIGIN(RAM) + LENGTH(RAM),
       "interrupt stack must end at the RAM top")
ASSERT(__gdr_irq_stack_low >= _ebss,
       "interrupt stack overlaps .bss")
ASSERT(__gdr_irq_stack_low >= gdr_heap_limit,
       "interrupt stack overlaps the heap reserve")
