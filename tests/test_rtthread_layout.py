"""Unit tests for RT-Thread layout metadata."""

from __future__ import annotations

import rtthread.adapter as adapter
import rtthread.commands as commands
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


def test_legacy_31_profile_uses_pre_null_object_codes_and_flags_field():
    """3.1.0-3.1.2 predate the ``Null = 0`` object enum entry."""
    layout = build_layouts(RtConfig(using_semaphore=True), (3, 1, 0))

    assert layout.object_codes["thread"] == 0
    assert layout.object_codes["semaphore"] == 1
    assert layout.structs["struct rt_thread"].fields["flag"].path == ("flags",)
    assert layout.structs["struct rt_thread"].fields["stat"].path == ("stat",)
    assert "reserved" not in layout.structs["struct rt_semaphore"].fields
    assert commands._parse_type_name("semaphore", layout) == 1
    assert commands._parse_type_name("SEMAPHORE", layout) == 1
    assert adapter._type_code(layout, "semaphore") == 1


def test_resolve_object_type_code_accepts_display_and_semantic_names():
    """Type-name lookup is case-insensitive for command and convenience use."""
    from rtthread.layout import resolve_object_type_code

    layout = build_layouts(RtConfig(using_semaphore=True), (4, 0, 5))

    assert resolve_object_type_code("SEMAPHORE", layout) == 2
    assert resolve_object_type_code("semaphore", layout) == 2
    assert resolve_object_type_code("MSGQUEUE", layout) == 6
    assert resolve_object_type_code("missing", layout) is None


def test_modern_31_profile_uses_null_shifted_object_codes():
    """3.1.3 retains its enum migration and added semaphore field."""
    layout = build_layouts(
        RtConfig(using_semaphore=True, using_soft_timer=True), (3, 1, 3)
    )

    assert layout.object_codes["thread"] == 1
    assert layout.object_codes["semaphore"] == 2
    assert layout.object_types[2].name == "semaphore"
    assert "reserved" in layout.structs["struct rt_semaphore"].fields
    assert layout.list_hooks["timer_list"].head_expr == "rt_timer_list[0]"
    assert layout.list_hooks["soft_timer_list"].head_expr == "rt_soft_timer_list[0]"


def test_4x_soft_timer_hook_requires_its_own_config_probe():
    """A device-enabled target alone does not prove soft timers are compiled."""
    no_soft = build_layouts(RtConfig(using_device=True), (4, 0, 5))
    soft = build_layouts(RtConfig(using_soft_timer=True), (4, 0, 5))

    assert "soft_timer_list" not in no_soft.list_hooks
    assert soft.list_hooks["soft_timer_list"].head_expr == "_soft_timer_list[0]"
