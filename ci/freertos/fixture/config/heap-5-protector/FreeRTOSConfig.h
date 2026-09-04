/* GDR fixture variant: heap-5-protector.
 *
 * heap_5 linked together with configENABLE_HEAP_PROTECTOR (V11+ only), so
 * the pucHeapLowAddress/pucHeapHighAddress region extremes exist and the
 * linear walk + CrossCheck get their first live heap_5 evidence.  The
 * fixture registers exactly ONE heap region: with multiple regions the
 * extremes would span gaps and TotalSize would be a lie.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configENABLE_HEAP_PROTECTOR 1
#define GDR_FIXTURE_HEAP_5 1
#define GDR_FIXTURE_HEAP_5_SINGLE_REGION 1

#include "gdr_fixture_common.h"

#endif
