/* GDR fixture variant: static-dynamic.
 *
 * Both allocators on: the only combination that emits ucStaticallyAllocated.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configSUPPORT_STATIC_ALLOCATION 1
#define configSUPPORT_DYNAMIC_ALLOCATION 1
#define GDR_FIXTURE_MIXED_ALLOCATION 1

#include "gdr_fixture_common.h"

#endif
