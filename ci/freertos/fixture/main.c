/* GDR FreeRTOS QEMU fixture for the B-L475E-IOT01A Cortex-M4 target. */
#include "FreeRTOS.h"
#include "queue.h"
#include "semphr.h"
#include "task.h"
#include "timers.h"

#define GDR_USED __attribute__((used))

extern void xPortSysTickHandler(void);

GDR_USED static TaskHandle_t gdr_high_task;
GDR_USED static TaskHandle_t gdr_normal_task;
GDR_USED static TaskHandle_t gdr_low_task;
GDR_USED static QueueHandle_t gdr_registered_queue;
GDR_USED static QueueHandle_t gdr_unregistered_queue;
GDR_USED static SemaphoreHandle_t gdr_semaphore;
GDR_USED static SemaphoreHandle_t gdr_mutex;
GDR_USED static TimerHandle_t gdr_active_timer;
GDR_USED static TimerHandle_t gdr_inactive_timer;

static void gdr_semihosting_write0(const char *message)
{
    register int operation __asm__("r0") = 0x04;
    register const char *argument __asm__("r1") = message;
    __asm__ volatile("bkpt 0xab" : : "r"(operation), "r"(argument) : "memory");
}

void gdr_fixture_assert_failed(int line)
{
    (void)line;
    taskDISABLE_INTERRUPTS();
    for (;;) {
    }
}

void vApplicationMallocFailedHook(void)
{
    gdr_fixture_assert_failed(__LINE__);
}

static void gdr_timer_callback(TimerHandle_t timer)
{
    (void)timer;
}

static void gdr_idle_task(void *argument)
{
    (void)argument;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

static void gdr_ready_task(void *argument)
{
    (void)argument;
    /* Let the timer daemon consume xTimerStart before declaring the fixture ready. */
    vTaskDelay(pdMS_TO_TICKS(20));
    gdr_semihosting_write0("GDR FreeRTOS fixture ready.\n");
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}

void SysTick_Handler(void)
{
    xPortSysTickHandler();
}

int main(void)
{
    gdr_registered_queue = xQueueCreate(4, sizeof(uint32_t));
    gdr_unregistered_queue = xQueueCreate(2, sizeof(uint32_t));
    gdr_semaphore = xSemaphoreCreateCounting(3, 1);
    gdr_mutex = xSemaphoreCreateMutex();
    gdr_active_timer = xTimerCreate("gdr_active", pdMS_TO_TICKS(100), pdTRUE,
                                    NULL, gdr_timer_callback);
    gdr_inactive_timer = xTimerCreate("gdr_idle", pdMS_TO_TICKS(100), pdFALSE,
                                      NULL, gdr_timer_callback);

    configASSERT(gdr_registered_queue != NULL);
    configASSERT(gdr_unregistered_queue != NULL);
    configASSERT(gdr_semaphore != NULL);
    configASSERT(gdr_mutex != NULL);
    configASSERT(gdr_active_timer != NULL);
    configASSERT(gdr_inactive_timer != NULL);

    vQueueAddToRegistry(gdr_registered_queue, "gdr_queue");
    vQueueAddToRegistry(gdr_semaphore, "gdr_semaphore");
    vQueueAddToRegistry(gdr_mutex, "gdr_mutex");
    configASSERT(xTimerStart(gdr_active_timer, 0) == pdPASS);

    configASSERT(xTaskCreate(gdr_ready_task, "gdr_ready", configMINIMAL_STACK_SIZE,
                             NULL, 4, &gdr_high_task) == pdPASS);
    configASSERT(xTaskCreate(gdr_idle_task, "gdr_normal", configMINIMAL_STACK_SIZE,
                             NULL, 2, &gdr_normal_task) == pdPASS);
    configASSERT(xTaskCreate(gdr_idle_task, "gdr_low", configMINIMAL_STACK_SIZE,
                             NULL, 1, &gdr_low_task) == pdPASS);

    vTaskStartScheduler();
    gdr_fixture_assert_failed(__LINE__);
    return 0;
}
