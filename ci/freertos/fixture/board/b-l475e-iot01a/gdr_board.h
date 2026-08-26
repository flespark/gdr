/* B-L475E-IOT01A board glue for the GDR FreeRTOS fixture. */
#ifndef GDR_BOARD_H
#define GDR_BOARD_H

#include "stm32l475xx.h"

extern uint32_t SystemCoreClock;
#define configCPU_CLOCK_HZ (SystemCoreClock)

#endif
