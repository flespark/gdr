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
GDR_USED volatile UBaseType_t uxCurrentNumberOfTasks = 4U;
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
EMPTY_LIST(xTasksWaitingTermination);

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
