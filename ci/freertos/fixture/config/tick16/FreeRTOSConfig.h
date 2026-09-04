/* GDR fixture variant: tick16.
 *
 * Forces a 16-bit TickType_t (later kernels spell the knob
 * configTICK_TYPE_WIDTH_IN_BITS; the legacy configUSE_16_BIT_TICKS survives
 * in every supported kernel and FreeRTOS.h maps it into the new enum), so
 * sizeof(TickType_t) == sizeof(EventBits_t) == 2 and the event-group
 * control bits move to the 0xff00 byte -- the 16-bit cell for the
 * tick-width-derived masks, portMAX_DELAY sentinels and the ListInit /
 * NextUnblockTime raw reads.
 *
 * The legacy knob must be defined BEFORE the common header: the shared
 * header's #ifndef guard would otherwise re-instate its own
 * configUSE_16_BIT_TICKS 0, and FreeRTOS.h rejects a config that defines
 * both the old and the new knob (so the rv64 variant's #undef + new-macro
 * pattern must not be copied here -- configTICK_TYPE_WIDTH_IN_BITS only
 * exists from V10.6.0, which would break the 10.3.1 config lane).
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configUSE_16_BIT_TICKS 1

#include "gdr_fixture_common.h"

#endif
