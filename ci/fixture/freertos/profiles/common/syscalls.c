/* Minimal newlib syscalls shared by the kernel-direct boards.
 *
 * Provides a sbrk arena (bounded by the gdr_heap_limit symbol each board's
 * test codes defines) so heap_3 wraps a real malloc instead of failing at
 * link time.  The RISC-V board needs the same arena as the ARM boards; the
 * per-board comment header (which previously only differed cosmetically) is
 * not duplicated. */
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>
#include <errno.h>

extern uint8_t end;
extern uint8_t gdr_heap_limit;

void *_sbrk(ptrdiff_t increment)
{
    static uint8_t *heap = &end;
    uint8_t *previous = heap;
    uint8_t *next = heap + increment;
    if (next > &gdr_heap_limit) {
        errno = ENOMEM;
        return (void *)-1;
    }
    heap = next;
    return previous;
}

/* Reason: -funwind-tables makes .ARM.exidx reference the EHABI personality
 * routines.  The fixture has no C++ runtime; these local no-op definitions
 * satisfy the link without pulling libgcc's unwind-arm.o (which would also
 * require __exidx_start/__exidx_end and libc process syscalls). */
void __aeabi_unwind_cpp_pr0(void)
{
}

void __aeabi_unwind_cpp_pr1(void)
{
}

void __aeabi_unwind_cpp_pr2(void)
{
}
