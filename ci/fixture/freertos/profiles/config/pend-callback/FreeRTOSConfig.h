/* GDR fixture variant: pend-callback.
 *
 * Enables INCLUDE_xTimerPendFunctionCall so the daemon queue message union
 * carries u.xCallbackParameters (timers.c), which the GDR timer command
 * decoder distinguishes by DWARF member presence.  The fixture suspends the
 * timer daemon before enqueueing a pended callback plus several timer
 * commands, so the negative-ID slot and a non-empty (ring-wrapping) command
 * queue survive until the harness breakpoint.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define INCLUDE_xTimerPendFunctionCall 1

#include "gdr_fixture_common.h"

#endif
