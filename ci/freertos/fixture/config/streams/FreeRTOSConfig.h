/* GDR fixture variant: streams.
 *
 * Both allocators on (so a statically created buffer can be deleted while
 * the handle symbol survives) plus GDR_FIXTURE_STREAM_ACTIONS, which makes
 * the fixture actually write/read/delete its stream/message/batching
 * buffers before the ready marker.  Runs on mps2-an385/11.1.0 (or any
 * V11.1+ kernel-direct lane) so the batching buffer exists
 * (GDR_HAS_BATCHING_BUFFER).
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configSUPPORT_STATIC_ALLOCATION 1
#define configSUPPORT_DYNAMIC_ALLOCATION 1
#define GDR_FIXTURE_MIXED_ALLOCATION 1
#define GDR_FIXTURE_STREAM_ACTIONS 1

#include "gdr_fixture_common.h"

#endif
