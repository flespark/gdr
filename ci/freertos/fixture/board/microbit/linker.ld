/* QEMU micro:bit fixture: 256 KiB flash and SRAM expanded to 256 KiB. */
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);
__gdr_armv6m_irq_count = 32;

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 256K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 256K
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

    .heap (NOLOAD) :
    {
        PROVIDE(end = .);
        . = . + 1K;
        gdr_heap_limit = .;
    } > RAM

    .irq_stack ORIGIN(RAM) + LENGTH(RAM) - 4K (NOLOAD) :
    {
        __gdr_irq_stack_low = .;
        . += 4K;
        __gdr_irq_stack_top = .;
    } > RAM
}

ASSERT(__gdr_irq_stack_top == _estack, "interrupt stack must end at RAM top")
ASSERT(__gdr_irq_stack_low >= _ebss, "interrupt stack overlaps .bss")
ASSERT(__gdr_irq_stack_low >= gdr_heap_limit, "interrupt stack overlaps heap")
