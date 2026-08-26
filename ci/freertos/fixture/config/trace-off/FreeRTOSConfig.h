/* GDR fixture variant: trace-off.
 *
 * configUSE_TRACE_FACILITY=0: ucQueueType is absent; queue classification
 * must fall back to the xQUEUE typedef or report no type.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configUSE_TRACE_FACILITY 0

#include "gdr_fixture_common.h"

#endif
