"""Unit tests for RT-Thread layout metadata."""

from __future__ import annotations

from rtthread.layout import (
    RT_OBJECT_CLASS_THREAD,
    RtConfig,
    ThreadState,
    build_layouts,
)


def test_layout_retains_probed_stack_direction():
    """The adapter passes its target stack direction into the generic layout."""
    assert build_layouts(RtConfig(stack_grows_up=True)).stack_grows_up is True


def test_thread_state_masks_flags_and_handles_unknown_values():
    """RT-Thread state decoding belongs to the RT-Thread adapter."""
    assert ThreadState.from_raw(0x83) is ThreadState.RUNNING
    assert ThreadState.from_raw(0x07) is ThreadState.UNKNOWN


def test_layout_supplies_display_and_intrusive_list_metadata():
    """Core printers and traversal consume metadata rather than RT-Thread names."""
    layout = build_layouts(RtConfig(using_mutex=True))

    assert layout.structs["struct rt_thread"].display_name == "Thread"
    assert layout.structs["struct rt_mutex"].fields["owner"].pointee_string_path == (
        "name",
    )
    assert layout.object_types[RT_OBJECT_CLASS_THREAD].next_path == ("next",)
