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
 *
 * MPU layout notes (the mpu variant, configENABLE_MPU 1):
 * - The kernel marks its privileged entry points PRIVILEGED_FUNCTION /
 *   FREERTOS_SYSTEM_CALL / PRIVILEGED_DATA, and mpu_wrappers.h declares
 *   them with __attribute__((section("privileged_functions"))) etc. -- the
 *   GCC section NAMES HAVE NO LEADING DOT.  The script must collect
 *   *(privileged_functions*), not *(.privileged_functions*): the dotted
 *   spelling matches nothing, the real section becomes an orphan, and the
 *   port's SVC gate (port.c: if (ulPC >= start && ulPC <= end)) sees an
 *   empty region and never starts the scheduler.
 * - ARMv8-M PMSAv8 forbids overlapping MPU regions.  The three flash
 *   windows (privileged functions, system calls, unprivileged task code)
 *   must therefore be disjoint, not three aliases of the whole FLASH
 *   window: overlapping regions fault with UFSR.INVPC in prvSetupMPU.
 * - __*_end__ symbols are the RLAR inclusive limit, so they are `start
 *   + size - 1` (the port writes them straight into the RLAR register).
 * On the non-MPU variants (smp) the privileged/system-call sections are
 * empty; their region symbols then span nothing and the port (compiled
 * without the MPU region setup) ignores them.
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

    /* Privileged entry points (PRIVILEGED_FUNCTION) -- region 0 of the
     * port's MPU setup (port.c prvSetupMPU case for the SVC handlers). */
    .privileged_functions : ALIGN(32)
    {
        __privileged_functions_start__ = .;
        KEEP(*(privileged_functions*))
        . = ALIGN(32);
        __privileged_functions_end__ = . - 1;
    } > FLASH

    /* SVC system-call trampolines (FREERTOS_SYSTEM_CALL) -- region 1. */
    .freertos_system_calls : ALIGN(32)
    {
        __syscalls_flash_start__ = .;
        KEEP(*(freertos_system_calls*))
        . = ALIGN(32);
        __syscalls_flash_end__ = . - 1;
    } > FLASH

    /* Task code runs unprivileged: everything after the privileged regions
     * is the unprivileged flash window (region 4 of prvSetupMPU). */
    .text : ALIGN(32)
    {
        __unprivileged_flash_start__ = .;
        *(.text*)
        *(.rodata*)
        . = ALIGN(32);
        _etext = .;
    } > FLASH
    __unprivileged_flash_end__ = ORIGIN(FLASH) + LENGTH(FLASH) - 1;

    /* PRIVILEGED_DATA globals live at the start of RAM so the port's
     * privileged-SRAM region (xPortIsAuthorizedToAccessBuffer) covers just
     * them, not the whole data window.  This section is the first loadable
     * data section, so _sidata points at its flash image and the startup
     * copy covers .privileged_data + .data. */
    .privileged_data : ALIGN(32)
    {
        _sdata = .;
        __privileged_sram_start__ = .;
        *(privileged_data*)
        . = ALIGN(32);
        __privileged_sram_end__ = . - 1;
    } > RAM AT > FLASH

    _sidata = LOADADDR(.privileged_data);

    .data : ALIGN(4)
    {
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
