/* GDR fixture variant: registry-0.
 *
 * configQUEUE_REGISTRY_SIZE=0: xQueueRegistry is not compiled.
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#include "gdr_board.h"

#define configQUEUE_REGISTRY_SIZE 0

#include "gdr_fixture_common.h"

#endif
