/* Static heap-only snapshot for the allocated-bit negative case.
 *
 * A healthy kernel never puts a block with the size_t MSB (the heap_4
 * allocation bit) on the free list: heapFREE_BLOCK clears the bit before
 * insertion (heap_4.c).  The main snapshot ELF carries the *mismatch* cell
 * (free-list vs linear vs counter disagree); this second ELF carries the
 * *corrupt-walk* cell, because one heap symbol set can only show one
 * corruption and cross_validate refuses to compare numbers over a corrupt
 * walk (heap.py).  Everything else the adapter needs is absent, so only the
 * heap lanes of `frt heap` / `frt system` are exercised here.
 */
#include <stddef.h>
#include <stdint.h>

#define GDR_USED __attribute__((used))

typedef struct A_BLOCK_LINK {
    struct A_BLOCK_LINK *pxNextFreeBlock;
    size_t xBlockSize;
} BlockLink_t;

/* blockA carries the allocation bit in xBlockSize: the free-list walk must
 * stop with "free-list member ... carries the allocated bit" and CrossCheck
 * must degrade to "unavailable: corrupt walk" instead of comparing numbers
 * over corrupted memory.  The linear walk still tiles the extent (size 16,
 * allocated), so it reports one allocated block. */
GDR_USED __attribute__((aligned(8))) struct {
    BlockLink_t block;  /* free-list head: size_t MSB set + 16-byte block */
    uint8_t pad[8];     /* payload of the 16-byte block */
    uint8_t tail[8];    /* pxEnd = block + 16, inside the symbol */
} ucHeap = {
    .block = {
        .pxNextFreeBlock = (BlockLink_t *)&ucHeap.tail[0],
        .xBlockSize = 0x80000010UL,
    },
};

GDR_USED BlockLink_t xStart = {
    .pxNextFreeBlock = &ucHeap.block,
    .xBlockSize = 0,
};
GDR_USED BlockLink_t *pxEnd = (BlockLink_t *)&ucHeap.tail[0];
GDR_USED size_t xFreeBytesRemaining = 16;

void _start(void)
{
    for (;;) {
    }
}
