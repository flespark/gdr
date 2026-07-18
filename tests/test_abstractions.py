"""Unit tests for RTOS-agnostic object abstractions."""

from __future__ import annotations

from gdr.abstractions import Thread


def test_thread_stack_used_uses_a_valid_downward_stack_pointer():
    """Stack consumption is derived only from a saved SP inside the stack."""
    assert (
        Thread(
            stack_addr=0x1000, stack_size=0x100, sp=0x1080, stack_grows_up=False
        ).stack_used
        == 0x80
    )
    assert (
        Thread(
            stack_addr=0x1000, stack_size=0x100, sp=0x0FFF, stack_grows_up=False
        ).stack_used
        is None
    )
    assert (
        Thread(
            stack_addr=0x1000, stack_size=0x100, sp=0x1101, stack_grows_up=False
        ).stack_used
        is None
    )


def test_thread_stack_used_supports_upward_stack_growth():
    """Upward stacks measure consumed space from the low stack address."""
    thread = Thread(stack_addr=0x1000, stack_size=0x100, sp=0x1080, stack_grows_up=True)

    assert thread.stack_used == 0x80


def test_thread_stack_used_is_unavailable_without_a_known_direction():
    """An unknown direction must not be guessed from the saved stack pointer."""
    assert Thread(stack_addr=0x1000, stack_size=0x100, sp=0x1080).stack_used is None
