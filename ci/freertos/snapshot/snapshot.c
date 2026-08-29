/* Static SMP snapshot: real List_t DWARF from list.h, TCB tag matching
 * tasks.c, unique globals so we never clash with a compiled kernel.
 *
 * Reason: #include "tasks.c" would emit the real pxCurrentTCBs / ready
 * lists into .bss (zero). file-only GDB reads .bss as zeros,
 * so the snapshot data has to live in our own .data symbols instead.
 */
#include "FreeRTOS.h"
#include "list.h"
#include "task.h"
#include "timers.h"  /* TimerCallbackFunction_t / TimerHandle_t typedefs */

#define GDR_USED __attribute__((used))
#define TASK_NAME_LEN configMAX_TASK_NAME_LEN
#define STACK_WORDS 16
/* Reason: tskSTACK_FILL_BYTE is a *byte* pattern; the kernel memsets the
 * whole stack with 0xa5, so an untouched StackType_t word reads
 * 0xa5a5a5a5. A word holding plain 0xa5 would make the high-water scan
 * stop after one byte and report 0 free words, i.e. a friendlier-than-real
 * fake that hides watermark-scan regressions. */
#define FILL 0xa5a5a5a5UL

/* Complete the TCB with the SMP / stats members GDR decodes. The tag
 * name matches tasks.c so lookup_type("struct tskTaskControlBlock") hits. */
struct tskTaskControlBlock {
    volatile StackType_t *pxTopOfStack;
    ListItem_t xStateListItem;
    ListItem_t xEventListItem;
    UBaseType_t uxPriority;
    StackType_t *pxStack;
    volatile BaseType_t xTaskRunState;
    UBaseType_t uxTaskAttributes;
    char pcTaskName[TASK_NAME_LEN];
    UBaseType_t uxCoreAffinityMask;
    BaseType_t xPreemptionDisable;
    StackType_t *pxEndOfStack;
    UBaseType_t uxBasePriority;
    UBaseType_t uxMutexesHeld;
    configRUN_TIME_COUNTER_TYPE ulRunTimeCounter;
    uint32_t ulNotifiedValue[1];
    uint8_t ucNotifyState[1];
};
typedef struct tskTaskControlBlock TCB_t;

#define IDLE_ATTR ((UBaseType_t)1U)

GDR_USED StackType_t gdr_stack0[STACK_WORDS] = {
    FILL, FILL, FILL, FILL, FILL, FILL, FILL, FILL,
    FILL, FILL, FILL, FILL, 0x11, 0x22, 0x33, 0x44,
};
GDR_USED StackType_t gdr_stack1[STACK_WORDS] = {
    FILL, FILL, FILL, FILL, FILL, FILL, FILL, FILL,
    FILL, FILL, FILL, FILL, 0x11, 0x22, 0x33, 0x44,
};
GDR_USED StackType_t gdr_stack_yield[STACK_WORDS] = {
    FILL, FILL, FILL, FILL, FILL, FILL, FILL, FILL,
    FILL, FILL, FILL, FILL, 0x11, 0x22, 0x33, 0x44,
};
GDR_USED StackType_t gdr_stack_ready[STACK_WORDS] = {
    FILL, FILL, FILL, FILL, FILL, FILL, FILL, FILL,
    FILL, FILL, FILL, FILL, 0x11, 0x22, 0x33, 0x44,
};

/* Forward declarations so list links and TCB owners can close the cycle. */
extern TCB_t gdr_tcb_core0;
extern TCB_t gdr_tcb_core1;
extern TCB_t gdr_tcb_yield;
extern TCB_t gdr_tcb_ready;
/* Negative-fixture forwards: timer epoch lists, corrupt-list items and the
 * four negative TCBs all close cycles across the definitions below. */
extern List_t xActiveTimerList1;
extern List_t xActiveTimerList2;
extern ListItem_t gdr_bad_item_a;
extern ListItem_t gdr_bad_item_b;
extern ListItem_t gdr_bad_item_c;
extern ListItem_t gdr_bad_item_d;
extern ListItem_t gdr_bad_item_e;
extern TCB_t gdr_tcb_negcycle;
extern TCB_t gdr_tcb_negcount;
extern TCB_t gdr_tcb_negindex;
extern TCB_t gdr_tcb_negoff;
extern List_t pxReadyTasksLists[configMAX_PRIORITIES];
extern List_t xDelayedTaskList1;
extern List_t xDelayedTaskList2;
extern List_t xPendingReadyList;
extern List_t xSuspendedTaskList;
extern List_t xTasksWaitingTermination;

GDR_USED TCB_t *volatile pxCurrentTCBs[configNUMBER_OF_CORES] = {
    &gdr_tcb_core0,
    &gdr_tcb_core1,
};
GDR_USED volatile BaseType_t xYieldPendings[configNUMBER_OF_CORES] = {0, 1};
GDR_USED TaskHandle_t xIdleTaskHandles[configNUMBER_OF_CORES] = {
    (TaskHandle_t)&gdr_tcb_core0,
    (TaskHandle_t)&gdr_tcb_core1,
};
GDR_USED volatile configRUN_TIME_COUNTER_TYPE ulTotalRunTime[configNUMBER_OF_CORES] = {
    1000U,
    3000U,
};
GDR_USED volatile BaseType_t xSchedulerRunning = pdTRUE;
GDR_USED volatile UBaseType_t uxCurrentNumberOfTasks = 8U;
/* Reason: a zero-initialized standalone global is placed in .bss (zero-init
 * optimization), which the file-only snapshot session cannot read; force
 * .data so the initializer bytes are actually present in the ELF. */
GDR_USED __attribute__((section(".data"))) volatile UBaseType_t uxSchedulerSuspended = 0U;
GDR_USED volatile TickType_t xNextTaskUnblockTime = 10U;
GDR_USED volatile TickType_t xTickCount = 42U;

#define EMPTY_LIST(name)                                                     \
    List_t name = {                                                          \
        .uxNumberOfItems = 0,                                                \
        .pxIndex = (ListItem_t *)&(name).xListEnd,                           \
        .xListEnd = {                                                        \
            .xItemValue = portMAX_DELAY,                                     \
            .pxNext = (ListItem_t *)&(name).xListEnd,                        \
            .pxPrevious = (ListItem_t *)&(name).xListEnd,                    \
        },                                                                   \
    }

EMPTY_LIST(xDelayedTaskList2);
EMPTY_LIST(xPendingReadyList);
EMPTY_LIST(xSuspendedTaskList);

GDR_USED List_t *volatile pxDelayedTaskList = &xDelayedTaskList1;
GDR_USED List_t *volatile pxOverflowDelayedTaskList = &xDelayedTaskList2;

GDR_USED List_t xDelayedTaskList1 = {
    .uxNumberOfItems = 1,
    .pxIndex = (ListItem_t *)&xDelayedTaskList1.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_tcb_yield.xStateListItem,
        .pxPrevious = &gdr_tcb_yield.xStateListItem,
    },
};

/* ------------------------------------------------------------------
 * Software timer subsystem (active-list epochs).  One timer sits on the
 * current list (expiry 100 > tick 42), one on the overflow list (expiry 5
 * < tick 42, so it belongs to the next tick epoch).  Live fixtures cannot
 * produce an overflow list without running for ~2^32 ticks, so the
 * overflow epoch formula ``(2^bits - tick) + expiry`` gets its live
 * evidence here (timers.c prvInsertTimerInActiveList).
 * ------------------------------------------------------------------ */
struct tmrTimerControl {
    const char *pcTimerName;
    ListItem_t xTimerListItem;
    TickType_t xTimerPeriodInTicks;
    void *pvTimerID;
    TimerCallbackFunction_t pxCallbackFunction;
    UBaseType_t uxTimerNumber; /* configUSE_TRACE_FACILITY == 1 (timers.c) */
    uint8_t ucStatus;
};
typedef struct tmrTimerControl Timer_t;

GDR_USED void gdr_timer_cb(TimerHandle_t xTimer)
{
    (void)xTimer;
}

GDR_USED char gdr_tmr_name_cur[] __attribute__((section(".data"))) = "gdr_tmr_cur";
GDR_USED char gdr_tmr_name_ovf[] __attribute__((section(".data"))) = "gdr_tmr_ovf";
/* Reason: the snapshot's .text is a handful of bytes, so a 64-byte string
 * window read from a rodata pointer crosses the section end and fails; the
 * names are forced into .data (RAM, non-const to avoid a section-type
 * conflict) where the window fits. */

extern Timer_t gdr_timer_current;
extern Timer_t gdr_timer_overflow;

GDR_USED List_t xActiveTimerList1 = {
    .uxNumberOfItems = 1,
    .pxIndex = (ListItem_t *)&xActiveTimerList1.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_timer_current.xTimerListItem,
        .pxPrevious = &gdr_timer_current.xTimerListItem,
    },
};

GDR_USED List_t xActiveTimerList2 = {
    .uxNumberOfItems = 1,
    .pxIndex = (ListItem_t *)&xActiveTimerList2.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_timer_overflow.xTimerListItem,
        .pxPrevious = &gdr_timer_overflow.xTimerListItem,
    },
};

GDR_USED List_t *volatile pxCurrentTimerList = &xActiveTimerList1;
GDR_USED List_t *volatile pxOverflowTimerList = &xActiveTimerList2;
/* Reason: the timer subsystem gate (cfg.timers) accepts either xTimerTaskHandle
 * or xTimerQueue; providing the handle avoids defining struct QueueDefinition,
 * which would open the queue discovery channel and the trace-facility probe. */
GDR_USED TaskHandle_t xTimerTaskHandle = (TaskHandle_t)&gdr_tcb_ready;

GDR_USED Timer_t gdr_timer_current = {
    .pcTimerName = gdr_tmr_name_cur,
    .xTimerListItem = {
        .xItemValue = 100,
        .pxNext = (ListItem_t *)&xActiveTimerList1.xListEnd,
        .pxPrevious = (ListItem_t *)&xActiveTimerList1.xListEnd,
        .pvOwner = &gdr_timer_current,
        .pxContainer = &xActiveTimerList1,
    },
    .xTimerPeriodInTicks = 100,
    .pvTimerID = NULL,
    .pxCallbackFunction = gdr_timer_cb,
    .ucStatus = 0x05, /* tmrSTATUS_IS_ACTIVE | tmrSTATUS_IS_AUTORELOAD */
};

GDR_USED Timer_t gdr_timer_overflow = {
    .pcTimerName = gdr_tmr_name_ovf,
    .xTimerListItem = {
        .xItemValue = 5,
        .pxNext = (ListItem_t *)&xActiveTimerList2.xListEnd,
        .pxPrevious = (ListItem_t *)&xActiveTimerList2.xListEnd,
        .pvOwner = &gdr_timer_overflow,
        .pxContainer = &xActiveTimerList2,
    },
    .xTimerPeriodInTicks = 10,
    .pvTimerID = NULL,
    .pxCallbackFunction = gdr_timer_cb,
    .ucStatus = 0x05,
};

/* ------------------------------------------------------------------
 * Corrupt-list negatives (gdr_bad_*).  These are standalone lists a
 * healthy kernel can never produce; the ``frt task <name>`` checks walk
 * each as the container of one negative TCB (which stays discoverable
 * through xTasksWaitingTermination).  They deliberately do not touch the
 * scheduler lists the existing snapshot assertions read.
 * ------------------------------------------------------------------ */
GDR_USED ListItem_t gdr_bad_item_a;
GDR_USED ListItem_t gdr_bad_item_b;
GDR_USED ListItem_t gdr_bad_item_c;
GDR_USED ListItem_t gdr_bad_item_d;
GDR_USED ListItem_t gdr_bad_item_e;

GDR_USED List_t gdr_bad_cycle = {
    .uxNumberOfItems = 2,
    .pxIndex = (ListItem_t *)&gdr_bad_cycle.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_bad_item_a,
        .pxPrevious = &gdr_bad_item_b,
    },
};

GDR_USED List_t gdr_bad_count = {
    .uxNumberOfItems = 3, /* three declared, only two walkable */
    .pxIndex = (ListItem_t *)&gdr_bad_count.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_bad_item_c,
        .pxPrevious = &gdr_bad_item_d,
    },
};

GDR_USED List_t gdr_bad_index = {
    .uxNumberOfItems = 1,
    /* Reason: pxIndex parked on a real item violates the SMP invariant that
     * it rests on &xListEnd between scheduler rotations. */
    .pxIndex = &gdr_bad_item_e,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_bad_item_e,
        .pxPrevious = &gdr_bad_item_e,
    },
};

GDR_USED List_t gdr_bad_offrange = {
    .uxNumberOfItems = 1,
    .pxIndex = (ListItem_t *)&gdr_bad_offrange.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = (ListItem_t *)0x10000000UL, /* inside no loadable section */
        .pxPrevious = (ListItem_t *)&gdr_bad_offrange.xListEnd,
    },
};

GDR_USED ListItem_t gdr_bad_item_a = {
    .xItemValue = 0x10,
    .pxNext = &gdr_bad_item_b,
    .pxPrevious = (ListItem_t *)&gdr_bad_cycle.xListEnd,
    .pvOwner = NULL,
    .pxContainer = &gdr_bad_cycle,
};
GDR_USED ListItem_t gdr_bad_item_b = {
    .xItemValue = 0x20,
    .pxNext = &gdr_bad_item_a, /* closes the cycle */
    .pxPrevious = &gdr_bad_item_a,
    .pvOwner = NULL,
    .pxContainer = &gdr_bad_cycle,
};
GDR_USED ListItem_t gdr_bad_item_c = {
    .xItemValue = 0x30,
    .pxNext = &gdr_bad_item_d,
    .pxPrevious = (ListItem_t *)&gdr_bad_count.xListEnd,
    .pvOwner = NULL,
    .pxContainer = &gdr_bad_count,
};
GDR_USED ListItem_t gdr_bad_item_d = {
    .xItemValue = 0x40,
    .pxNext = (ListItem_t *)&gdr_bad_count.xListEnd,
    .pxPrevious = &gdr_bad_item_c,
    .pvOwner = NULL,
    .pxContainer = &gdr_bad_count,
};
GDR_USED ListItem_t gdr_bad_item_e = {
    .xItemValue = 0x50,
    .pxNext = (ListItem_t *)&gdr_bad_index.xListEnd,
    .pxPrevious = (ListItem_t *)&gdr_bad_index.xListEnd,
    .pvOwner = NULL,
    .pxContainer = &gdr_bad_index,
};

/* Zero-filled stack: the StackFillPresent negative (this build prefills
 * task stacks, so an all-zero window is corruption, not a config choice).
 * Forced into .data so the zeros are actually readable in the file session. */
GDR_USED __attribute__((section(".data"))) StackType_t gdr_stack_neg[STACK_WORDS] = {0};

GDR_USED TCB_t gdr_tcb_negcycle;
GDR_USED TCB_t gdr_tcb_negcount;
GDR_USED TCB_t gdr_tcb_negindex;
GDR_USED TCB_t gdr_tcb_negoff;

/* The four negative TCBs are discoverable through xTasksWaitingTermination
 * (so ``frt task negcycle`` resolves them) while each claims one corrupt
 * container list in its state item. */
GDR_USED List_t xTasksWaitingTermination = {
    .uxNumberOfItems = 4,
    .pxIndex = (ListItem_t *)&xTasksWaitingTermination.xListEnd,
    .xListEnd = {
        .xItemValue = portMAX_DELAY,
        .pxNext = &gdr_tcb_negcycle.xStateListItem,
        .pxPrevious = &gdr_tcb_negoff.xStateListItem,
    },
};

#define NEG_TCB(name, container_addr, event_owner_addr, next_item, prev_item, wname) \
    GDR_USED TCB_t name = {                                                  \
        .pxTopOfStack = &gdr_stack_neg[8],                                   \
        .xStateListItem = {                                                  \
            .xItemValue = 0,                                                 \
            .pxNext = next_item,                                             \
            .pxPrevious = prev_item,                                         \
            .pvOwner = &name,                                                \
            .pxContainer = container_addr,                                   \
        },                                                                   \
        .xEventListItem = {.pxContainer = NULL, .pvOwner = event_owner_addr}, \
        .uxPriority = 0,                                                     \
        .pxStack = gdr_stack_neg,                                            \
        .xTaskRunState = -1,                                                 \
        .uxTaskAttributes = 0,                                               \
        .pcTaskName = wname,                                                 \
        .uxCoreAffinityMask = 0x3,                                           \
        .xPreemptionDisable = 0,                                             \
        .pxEndOfStack = &gdr_stack_neg[STACK_WORDS - 1],                     \
        .uxBasePriority = 0,                                                 \
        .ulRunTimeCounter = 0,                                               \
        .ulNotifiedValue = {0},                                              \
        .ucNotifyState = {0},                                                \
    }

NEG_TCB(
    gdr_tcb_negcycle,
    &gdr_bad_cycle,
    &gdr_tcb_negcount, /* wrong event-item owner: ItemOwner negative */
    &gdr_tcb_negcount.xStateListItem,
    (ListItem_t *)&xTasksWaitingTermination.xListEnd,
    "negcycle");
NEG_TCB(
    gdr_tcb_negcount,
    &gdr_bad_count,
    &gdr_tcb_negcount,
    &gdr_tcb_negindex.xStateListItem,
    &gdr_tcb_negcycle.xStateListItem,
    "negcount");
NEG_TCB(
    gdr_tcb_negindex,
    &gdr_bad_index,
    &gdr_tcb_negindex,
    &gdr_tcb_negoff.xStateListItem,
    &gdr_tcb_negcount.xStateListItem,
    "negindex");
NEG_TCB(
    gdr_tcb_negoff,
    &gdr_bad_offrange,
    &gdr_tcb_negoff,
    (ListItem_t *)&xTasksWaitingTermination.xListEnd,
    &gdr_tcb_negindex.xStateListItem,
    "negoff");

/* ------------------------------------------------------------------
 * heap_4 mismatch negative: free-list bytes (16) differ from the linear
 * walk's free bytes (48) differ from xFreeBytesRemaining (64).  The free
 * list skips blockA (a linear-free block), so the three numbers disagree
 * while both walks stay structurally clean -- the only way to reach the
 * ``mismatch:`` branch of cross_validate (heap.py).  A healthy kernel
 * keeps these equal, so this state needs pre-initialised .data.
 * ------------------------------------------------------------------ */
typedef struct A_BLOCK_LINK {
    struct A_BLOCK_LINK *pxNextFreeBlock;
    size_t xBlockSize;
} BlockLink_t;

GDR_USED __attribute__((aligned(8))) struct {
    BlockLink_t blockA; /* free per the linear walk, absent from the free list */
    uint8_t pad_a[24];  /* payload: header(8) + 24 = 32-byte block */
    BlockLink_t blockB; /* free and on the free list */
    uint8_t pad_b[8];   /* header(8) + 8 = 16-byte block */
    uint8_t tail[8];    /* pxEnd sits here: blockA(32) + blockB(16) = 48 */
} ucHeap = {
    .blockA = {.pxNextFreeBlock = NULL, .xBlockSize = 32},
    .blockB = {.pxNextFreeBlock = (BlockLink_t *)&ucHeap.tail[0], .xBlockSize = 16},
};

GDR_USED BlockLink_t xStart = {
    .pxNextFreeBlock = &ucHeap.blockB, /* the free list skips blockA */
    .xBlockSize = 0,
};
GDR_USED BlockLink_t *pxEnd = (BlockLink_t *)&ucHeap.tail[0];
GDR_USED size_t xFreeBytesRemaining = 64;

GDR_USED List_t pxReadyTasksLists[configMAX_PRIORITIES] = {
    [0] = {
        .uxNumberOfItems = 2,
        .pxIndex = (ListItem_t *)&pxReadyTasksLists[0].xListEnd,
        .xListEnd = {
            .xItemValue = portMAX_DELAY,
            .pxNext = &gdr_tcb_core0.xStateListItem,
            .pxPrevious = &gdr_tcb_core1.xStateListItem,
        },
    },
    [1] = {
        .uxNumberOfItems = 1,
        .pxIndex = (ListItem_t *)&pxReadyTasksLists[1].xListEnd,
        .xListEnd = {
            .xItemValue = portMAX_DELAY,
            .pxNext = &gdr_tcb_ready.xStateListItem,
            .pxPrevious = &gdr_tcb_ready.xStateListItem,
        },
    },
    [2] = {
        .uxNumberOfItems = 0,
        .pxIndex = (ListItem_t *)&pxReadyTasksLists[2].xListEnd,
        .xListEnd = {
            .xItemValue = portMAX_DELAY,
            .pxNext = (ListItem_t *)&pxReadyTasksLists[2].xListEnd,
            .pxPrevious = (ListItem_t *)&pxReadyTasksLists[2].xListEnd,
        },
    },
    [3] = {
        .uxNumberOfItems = 0,
        .pxIndex = (ListItem_t *)&pxReadyTasksLists[3].xListEnd,
        .xListEnd = {
            .xItemValue = portMAX_DELAY,
            .pxNext = (ListItem_t *)&pxReadyTasksLists[3].xListEnd,
            .pxPrevious = (ListItem_t *)&pxReadyTasksLists[3].xListEnd,
        },
    },
    [4] = {
        .uxNumberOfItems = 0,
        .pxIndex = (ListItem_t *)&pxReadyTasksLists[4].xListEnd,
        .xListEnd = {
            .xItemValue = portMAX_DELAY,
            .pxNext = (ListItem_t *)&pxReadyTasksLists[4].xListEnd,
            .pxPrevious = (ListItem_t *)&pxReadyTasksLists[4].xListEnd,
        },
    },
    [5] = {
        .uxNumberOfItems = 0,
        .pxIndex = (ListItem_t *)&pxReadyTasksLists[5].xListEnd,
        .xListEnd = {
            .xItemValue = portMAX_DELAY,
            .pxNext = (ListItem_t *)&pxReadyTasksLists[5].xListEnd,
            .pxPrevious = (ListItem_t *)&pxReadyTasksLists[5].xListEnd,
        },
    },
};

GDR_USED TCB_t gdr_tcb_core0 = {
    .pxTopOfStack = &gdr_stack0[12],
    .xStateListItem = {
        .xItemValue = 0,
        .pxNext = &gdr_tcb_core1.xStateListItem,
        .pxPrevious = (ListItem_t *)&pxReadyTasksLists[0].xListEnd,
        .pvOwner = &gdr_tcb_core0,
        .pxContainer = &pxReadyTasksLists[0],
    },
    .xEventListItem = {.pxContainer = NULL, .pvOwner = &gdr_tcb_core0},
    .uxPriority = 0,
    .pxStack = gdr_stack0,
    .xTaskRunState = 0,
    .uxTaskAttributes = IDLE_ATTR,
    .pcTaskName = "idle0",
    .uxCoreAffinityMask = 0x1,
    .xPreemptionDisable = 0,
    .pxEndOfStack = &gdr_stack0[STACK_WORDS - 1],
    .uxBasePriority = 0,
    .ulRunTimeCounter = 400,
    .ulNotifiedValue = {0},
    .ucNotifyState = {0},
};

GDR_USED TCB_t gdr_tcb_core1 = {
    .pxTopOfStack = &gdr_stack1[12],
    .xStateListItem = {
        .xItemValue = 0,
        .pxNext = (ListItem_t *)&pxReadyTasksLists[0].xListEnd,
        .pxPrevious = &gdr_tcb_core0.xStateListItem,
        .pvOwner = &gdr_tcb_core1,
        .pxContainer = &pxReadyTasksLists[0],
    },
    .xEventListItem = {.pxContainer = NULL, .pvOwner = &gdr_tcb_core1},
    .uxPriority = 0,
    .pxStack = gdr_stack1,
    .xTaskRunState = 1,
    .uxTaskAttributes = IDLE_ATTR,
    .pcTaskName = "idle1",
    .uxCoreAffinityMask = 0x2,
    .xPreemptionDisable = 0,
    .pxEndOfStack = &gdr_stack1[STACK_WORDS - 1],
    .uxBasePriority = 0,
    .ulRunTimeCounter = 800,
    .ulNotifiedValue = {0},
    .ucNotifyState = {0},
};

GDR_USED TCB_t gdr_tcb_yield = {
    .pxTopOfStack = &gdr_stack_yield[12],
    .xStateListItem = {
        .xItemValue = 10,
        .pxNext = (ListItem_t *)&xDelayedTaskList1.xListEnd,
        .pxPrevious = (ListItem_t *)&xDelayedTaskList1.xListEnd,
        .pvOwner = &gdr_tcb_yield,
        .pxContainer = &xDelayedTaskList1,
    },
    .xEventListItem = {.pxContainer = NULL, .pvOwner = &gdr_tcb_yield},
    .uxPriority = 2,
    .pxStack = gdr_stack_yield,
    /* -2, and this TCB is *not* in pxCurrentTCBs, so core_of() misses. */
    .xTaskRunState = -2,
    .uxTaskAttributes = 0,
    .pcTaskName = "yielder",
    .uxCoreAffinityMask = 0x3,
    .xPreemptionDisable = 1,
    .pxEndOfStack = &gdr_stack_yield[STACK_WORDS - 1],
    .uxBasePriority = 2,
    .ulRunTimeCounter = 200,
    .ulNotifiedValue = {0},
    .ucNotifyState = {0},
};

GDR_USED TCB_t gdr_tcb_ready = {
    .pxTopOfStack = &gdr_stack_ready[12],
    .xStateListItem = {
        .xItemValue = 0,
        .pxNext = (ListItem_t *)&pxReadyTasksLists[1].xListEnd,
        .pxPrevious = (ListItem_t *)&pxReadyTasksLists[1].xListEnd,
        .pvOwner = &gdr_tcb_ready,
        .pxContainer = &pxReadyTasksLists[1],
    },
    .xEventListItem = {.pxContainer = NULL, .pvOwner = &gdr_tcb_ready},
    .uxPriority = 1,
    .pxStack = gdr_stack_ready,
    .xTaskRunState = -1,
    .uxTaskAttributes = 0,
    .pcTaskName = "ready0",
    .uxCoreAffinityMask = 0x3,
    .xPreemptionDisable = 0,
    .pxEndOfStack = &gdr_stack_ready[STACK_WORDS - 1],
    .uxBasePriority = 1,
    .ulRunTimeCounter = 100,
    .ulNotifiedValue = {0},
    .ucNotifyState = {0},
};

void _start(void)
{
    for (;;) {
    }
}
