/* Bump allocator so heap_3 can link without libc (CubeL4 discards libc.a). */
#include <stddef.h>
#include <stdint.h>

#define GDR_HEAP3_BYTES (16 * 1024)

static uint8_t gdr_heap3_arena[GDR_HEAP3_BYTES];
static size_t gdr_heap3_used;

void *malloc(size_t size)
{
    size_t aligned = (size + 7U) & ~((size_t)7U);
    if (gdr_heap3_used + aligned > sizeof(gdr_heap3_arena)) {
        return NULL;
    }
    void *block = &gdr_heap3_arena[gdr_heap3_used];
    gdr_heap3_used += aligned;
    return block;
}

void free(void *pointer)
{
    (void)pointer;
}
