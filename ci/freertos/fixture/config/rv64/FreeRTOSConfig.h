/* GDR fixture variant: rv64 (the 64-bit lane's config).
 *
 * The RISC-V port hardcodes TickType_t to the architecture width (64), but
 * the kernel's tick-width *constants* follow configTICK_TYPE_WIDTH_IN_BITS,
 * which defaults to 32 -- leaving EventBits_t 64-bit while its control bits
 * sit in bits 24..31, which event-groups.c packs into a 64-bit item value.
 * Declaring the 64-bit tick width makes sizeof(TickType_t) agree with the
 * kernel's control-byte placement (top byte), which is what the event-group
 * decode and the tick-width-derived masks assume.
 *
 * FreeRTOS.h rejects a config that defines both configUSE_16_BIT_TICKS (the
 * shared common header's default) and configTICK_TYPE_WIDTH_IN_BITS, so the
 * 16-bit knob is cancelled after the common header runs.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#include "gdr_fixture_common.h"

#undef configUSE_16_BIT_TICKS
#define configTICK_TYPE_WIDTH_IN_BITS TICK_TYPE_WIDTH_64_BITS

#endif
