/* GDR fixture variant: static-only.
 *
 * configSUPPORT_STATIC_ALLOCATION=1 and DYNAMIC=0: xTaskCreateStatic exists
 * but ucStaticallyAllocated is not compiled into the TCB.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configSUPPORT_STATIC_ALLOCATION 1
#define configSUPPORT_DYNAMIC_ALLOCATION 0

#include "gdr_fixture_common.h"

#endif
