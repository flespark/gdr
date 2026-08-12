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
    # Blocking-waiter fixture objects (created only on fixture revisions that
    # include the PLAN 11.10 deterministic blocking threads).
    wait_semaphore_name: str = "wait_sem"
    wait_mutex_name: str = "wait_mutex"
    wait_event_name: str = "wait_event"
    wait_mailbox_recv_name: str = "wait_mb_recv"
    wait_mailbox_send_name: str = "wait_mb_send"
    wait_msgqueue_recv_name: str = "wait_mq_recv"
    wait_msgqueue_send_name: str = "wait_mq_send"
    wait_mempool_name: str = "wait_mp"
    locker_thread_name: str = "locker"
    sem_waiter_thread: str = "worker4"
    mutex_waiter_thread: str = "worker5"
    event_waiter_thread: str = "worker6"
    mailbox_recv_thread: str = "worker7"
    mailbox_send_thread: str = "worker8"
    msgqueue_recv_thread: str = "worker9"
    msgqueue_send_thread: str = "worker10"
    mempool_waiter_thread: str = "worker11"
    current_thread_expression: str = "rt_current_thread"
    mq_sender_list: bool = True


def _messagequeue_sender_list(major: int, minor: int, patch: int) -> bool:
    """Return whether a version provides ``rt_messagequeue.suspend_sender_thread``.

    The sender wait list was introduced in v3.1.4, dropped in v4.0.0-v4.0.1,
    and restored in v4.0.2. Kept independent of production layout metadata so
    a GDR regression cannot silently update the expected values.
    """
    if major == 3:
        return minor == 1 and patch >= 4
    return major == 4 and (minor >= 1 or (minor == 0 and patch >= 2))


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
        mq_sender_list=_messagequeue_sender_list(major, minor, patch),
    )
