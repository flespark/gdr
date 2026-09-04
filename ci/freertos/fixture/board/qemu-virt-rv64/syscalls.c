/* Minimal newlib syscalls for the RISC-V virt board (heap_3 sbrk arena). */
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

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
