"""Independent RT-Thread fixture expectations for QEMU integration tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RtThreadTestProfile:
    """Expected ABI and fixture contract for one RT-Thread test target."""

    semaphore_code: int
    mutex_code: int
    event_code: int
    mailbox_code: int
    msgqueue_code: int
    mempool_code: int
    timer_code: int
    semaphore_name: str = "test_sem"
    mutex_name: str = "test_mutex"
    event_name: str = "test_event"
    mailbox_name: str = "test_mailbox"
    msgqueue_name: str = "test_msgqueue"
    mempool_name: str = "test_mempool"
    timer_name: str = "test_timer"
    current_thread_expression: str = "rt_current_thread"


def get_rtthread_test_profile(version: str, target: str) -> RtThreadTestProfile:
    """Return expectations independent from GDR's production layout metadata."""
    major, minor, patch = (int(part) for part in version.split(".", 2))
    legacy_31 = (major, minor) == (3, 1) and patch <= 2
    offset = 0 if legacy_31 else 1
    current_thread_expression = (
        "rt_current_thread"
        if target == "rv64" or major == 3
        else "rt_cpu_index(rt_hw_cpu_id())->current_thread"
    )
    return RtThreadTestProfile(
        semaphore_code=0x01 + offset,
        mutex_code=0x02 + offset,
        event_code=0x03 + offset,
        mailbox_code=0x04 + offset,
        msgqueue_code=0x05 + offset,
        mempool_code=0x07 + offset,
        timer_code=0x09 + offset,
        current_thread_expression=current_thread_expression,
    )
