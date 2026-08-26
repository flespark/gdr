/* GDR fixture variant: heap-protector.
 *
 * configENABLE_HEAP_PROTECTOR is V11.0.0+; ignored by earlier kernels.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configENABLE_HEAP_PROTECTOR 1

#include "gdr_fixture_common.h"

#endif
