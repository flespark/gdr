"""Unit tests for RT-Thread QEMU fixture expectation profiles."""

from __future__ import annotations

from tests.support.rtthread_profiles import get_rtthread_test_profile


def test_legacy_31_profile_has_pre_null_object_codes():
    """3.1.0-3.1.2 use object codes before the Null enum entry."""
    profile = get_rtthread_test_profile("3.1.2", "cortex-a9")

    assert profile.semaphore_code == 0x01
    assert profile.mutex_code == 0x02
    assert profile.event_code == 0x03
    assert profile.mailbox_code == 0x04
    assert profile.msgqueue_code == 0x05
    assert profile.mempool_code == 0x07
    assert profile.timer_code == 0x09
    assert profile.semaphore_name == "test_sem"
    assert profile.mutex_name == "test_mutex"
    assert profile.event_name == "test_event"
    assert profile.mailbox_name == "test_mailbox"
    assert profile.msgqueue_name == "test_msgqueue"
    assert profile.mempool_name == "test_mempool"
    assert profile.timer_name == "test_timer"
    assert profile.current_thread_expression == "rt_current_thread"


def test_modern_31_profile_has_null_shifted_object_codes():
    """3.1.3 inserted the Null object enum member at zero."""
    profile = get_rtthread_test_profile("3.1.3", "cortex-a9")

    assert profile.semaphore_code == 0x02
    assert profile.mutex_code == 0x03
    assert profile.event_code == 0x04
    assert profile.mailbox_code == 0x05
    assert profile.msgqueue_code == 0x06
    assert profile.mempool_code == 0x08
    assert profile.timer_code == 0x0A
    assert profile.current_thread_expression == "rt_current_thread"


def test_modern_profile_has_null_shifted_codes_and_smp_current_thread():
    """Cortex-A9 4.x uses shifted codes and the per-CPU table as current thread."""
    profile = get_rtthread_test_profile("4.0.5", "cortex-a9")

    assert profile.semaphore_code == 0x02
    assert profile.mutex_code == 0x03
    assert profile.event_code == 0x04
    assert profile.mailbox_code == 0x05
    assert profile.msgqueue_code == 0x06
    assert profile.mempool_code == 0x08
    assert profile.timer_code == 0x0A
    assert profile.semaphore_name == "test_sem"
    assert profile.mutex_name == "test_mutex"
    assert profile.timer_name == "test_timer"
    assert profile.current_thread_expression == "_cpus"


def test_rv64_profile_uses_global_current_thread():
    """The RV64 fixture keeps the global current-thread handle."""
    profile = get_rtthread_test_profile("4.1.1", "rv64")

    assert profile.semaphore_code == 0x02
    assert profile.mutex_code == 0x03
    assert profile.event_code == 0x04
    assert profile.mailbox_code == 0x05
    assert profile.msgqueue_code == 0x06
    assert profile.mempool_code == 0x08
    assert profile.timer_code == 0x0A
    assert profile.mutex_name == "test_mutex"
    assert profile.timer_name == "test_timer"
    assert profile.current_thread_expression == "rt_current_thread"
