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
