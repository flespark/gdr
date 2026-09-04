"""FreeRTOS adapter for GDR.

Covers version/config probing, DWARF layouts, scheduler-list navigation,
task conversion, the six-channel kernel-object discovery model (registry /
symbol / active / mpu-pool / waiter / user), and the ``freertos`` command
tree (tasks/system/objects/heap plus the queue-family, timer, event-group
and stream-buffer tables and details).
"""
