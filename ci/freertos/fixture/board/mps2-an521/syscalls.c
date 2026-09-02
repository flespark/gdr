/* Minimal syscalls so heap_3 (newlib malloc) has a sbrk arena. */
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
