/* QEMU mps2-an385: 4 MiB flash at 0x00000000, 4 MiB RAM at 0x20000000. */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 4M
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 4M
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

    /* Reserved main-stack (MSP / interrupt) region.  The lower bound is an
     * explicit linker fact: _estack only names the top of RAM and cannot be
     * used to infer how much stack the handlers may consume. */
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
