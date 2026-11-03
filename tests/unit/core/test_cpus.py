"""ELF ISA and ABI evidence parsing tests."""

from __future__ import annotations

import pytest

from core.cpus import cpu_of, file_attributes
from core.stack_frame_parser import SavedStackContext
from core.unwind_meta import ElfImage
from tests.unit.core.test_unwind import _elf


def test_saved_context_validates_width_registers_and_tcb_sp_proof():
    base = dict(
        profile_id="test/armv7-m",
        pointer_size=4,
        registers=((4, 0),),
        pc_offset=4,
        lr_offset=8,
        status_offset=12,
        sp_offset=16,
        context_span=(0, 16),
        validation="live",
    )
    assert SavedStackContext(**base).profile_id == "test/armv7-m"
    with pytest.raises(ValueError, match="invalid saved-context profile"):
        SavedStackContext(**(base | {"pointer_size": 3}))
    with pytest.raises(ValueError, match="duplicate"):
        SavedStackContext(**(base | {"registers": ((4, 0), (4, 4))}))
    with pytest.raises(ValueError, match="TCB context"):
        SavedStackContext(**(base | {"context_in_tcb": True}))
    with pytest.raises(ValueError, match="unaligned"):
        SavedStackContext(**(base | {"pc_offset": 2}))


def _attributes(vendor: bytes, attrs: list[tuple[int, int | str]]) -> bytes:
    values = bytearray()
    for tag, value in attrs:
        values.extend(bytes([tag]))
        if isinstance(value, str) and tag in (4, 5):
            values.extend(value.encode() + b"\0")
        elif isinstance(value, str):
            values.append(ord(value))
        else:
            assert value < 128
            values.append(value)
    scope = b"\x01" + (len(values) + 5).to_bytes(4, "little") + values
    subsection = vendor + b"\0" + scope
    return b"A" + (len(subsection) + 4).to_bytes(4, "little") + subsection


def _arm_file(
    name: str, arch: int, profile: str = "M", extra: tuple[int, int] | None = None
) -> bytes:
    values = bytearray()
    attrs = [(5, name), (6, arch), (7, profile)]
    if extra is not None:
        attrs.insert(1, extra)
    for tag, value in attrs:
        values.append(tag)
        if isinstance(value, str) and tag in (4, 5):
            values.extend(value.encode() + b"\0")
        elif isinstance(value, str):
            values.append(ord(value))
        else:
            values.append(value)
    file_tag = b"\x01" + (len(values) + 5).to_bytes(4, "little") + values
    scope = b"\x01" + (len(file_tag) + 5).to_bytes(4, "little") + file_tag
    subsection = b"aeabi\0" + scope
    return b"A" + (len(subsection) + 4).to_bytes(4, "little") + subsection


def _image(machine: int, is_64: bool, attrs: bytes, flags: int = 0):
    section = ".ARM.attributes" if machine == 40 else ".riscv.attributes"
    image = ElfImage.from_bytes(
        _elf(
            is_64=is_64,
            endian="little",
            machine=machine,
            sections=[(section, 0, attrs, 0x70000003, 0)],
        )
    )
    assert image is not None
    return ElfImage(
        image.pointer_size,
        image.endian,
        image.machine,
        image.sections,
        image.symbols,
        flags,
        image.anchors,
    )


@pytest.mark.parametrize(
    "tag,expected",
    [
        (11, "armv6-m"),
        (12, "armv6-m"),
        (10, "armv7-m"),
        (13, "armv7e-m"),
        (16, "armv8-m.baseline"),
        (17, "armv8-m.mainline"),
    ],
)
def test_arm_isa_uses_arch_and_profile_attributes_not_cpu_name(tag, expected):
    blob = _attributes(b"aeabi", [(7, "M"), (5, "a vendor cpu name"), (6, tag)])
    result = cpu_of(_image(40, False, blob))
    assert (result.isa, result.abi, result.reason) == (expected, "aapcs32", "ok")


def test_attributes_accept_reordered_file_scope_and_skippable_unknowns():
    blob = _arm_file("cpu", 17, extra=(98, 7))
    assert file_attributes(blob, b"aeabi")[6] == 17
    assert cpu_of(_image(40, False, blob)).isa == "armv8-m.mainline"


def test_arm_attribute_files_merge_distinct_cpu_names_but_reject_arch_conflicts():
    def combined(first, second):
        sub = b"aeabi\0" + first[11:] + second[11:]
        return b"A" + (len(sub) + 4).to_bytes(4, "little") + sub

    merged = combined(_arm_file("cpu-a", 11), _arm_file("cpu-b", 12))
    assert cpu_of(_image(40, False, merged)).isa == "armv6-m"
    bad = _arm_file("cpu-b", 10, "A")
    assert file_attributes(combined(_arm_file("cpu-a", 11), bad), b"aeabi") is None


def test_arm_application_profile_is_not_m_profile():
    result = cpu_of(_image(40, False, _arm_file("cortex-a9", 10, "A")))
    assert result.isa == "armv7-a" and result.reason == "ok"


def test_attributes_reject_truncated_scopes_and_malformed_nested_file_records():
    valid = _arm_file("cpu", 17)
    for cut in range(1, len(valid)):
        assert file_attributes(valid[:cut], b"aeabi") is None
    malformed_file = b"\x01\x05\x00\x00\x00"
    subsection = (
        b"aeabi\0\x01"
        + (len(malformed_file) + 5).to_bytes(4, "little")
        + malformed_file
    )
    blob = b"A" + (len(subsection) + 4).to_bytes(4, "little") + subsection
    assert file_attributes(blob, b"aeabi") is None


def test_arm_compatibility_tag_is_length_prefixed_and_attributes_can_be_merged():
    attrs = bytearray(b"\x06\x0b\x07M\x20\x01\x0ecompatibility\0")
    scope = b"\x01" + (len(attrs) + 5).to_bytes(4, "little") + attrs
    subsection = b"aeabi\0" + scope
    blob = b"A" + (len(subsection) + 4).to_bytes(4, "little") + subsection
    assert cpu_of(_image(40, False, blob)).isa == "armv6-m"


@pytest.mark.parametrize("tag,value", [(7, "A"), (28, 1), (48, 1)])
def test_arm_attributes_missing_arch_profile_or_extensions_do_not_guess(tag, value):
    attrs = [(6, 17), (7, "M")]
    attrs = [(key, item) for key, item in attrs if key != tag] + [(tag, value)]
    result = cpu_of(_image(40, False, _attributes(b"aeabi", attrs)))
    if tag == 28:
        assert result.reason == "ok" and result.abi == "aapcs32-vfp"
        return
    assert result.reason in {
        "cpu-evidence-unavailable",
        "unsupported-cpu",
        "unsupported-extension-context",
    }


def test_attribute_parser_rejects_unknown_tags_when_they_conflict():
    blob = _attributes(b"riscv", [(5, "rv64i2p1_m2p0_a2p1_c2p0")])
    result = cpu_of(_image(243, True, blob))
    assert result.reason == "ok" and result.abi == "lp64"
    conflict = _attributes(b"riscv", [(5, "rv64i2p1_m2p0")])
    data = _attributes(b"riscv", [(4, 16)])[5:] + blob[5:] + conflict[5:]
    combined = b"A" + len(data).to_bytes(4, "little") + data
    assert file_attributes(combined, b"riscv") is None
    assert file_attributes(blob[:-1], b"riscv") is None


@pytest.mark.parametrize(
    "arch,flags,expected",
    [
        ("rv32imac", 0, "ilp32"),
        ("rv64imac", 0, "lp64"),
        ("rv32i2p1_m2p0_a2p1_c2p0", 0, "ilp32"),
        ("rv64i2p1_m2p0_a2p1_c2p0", 0, "lp64"),
        ("rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0", 4, "lp64d"),
    ],
)
def test_riscv_attributes_and_abi_flags_are_independent_of_port_layout(
    arch, flags, expected
):
    blob = _attributes(b"riscv", [(5, arch), (4, 16)])
    result = cpu_of(_image(243, arch.startswith("rv64"), blob, flags))
    assert result.reason == "ok" and result.abi == expected


def test_riscv_float_abi_is_reported_separately_from_integer_save_layout():
    from rtthread.stack_frame_profile import select_context

    blob = _attributes(b"riscv", [(5, "rv64i2p1_m2p0_a2p1_f2p2_d2p2_c2p0_zicsr2p0")])
    image = _image(243, True, blob, flags=5)
    result = cpu_of(image)
    assert result.abi == "lp64d" and result.reason == "ok"
    assert select_context(image).reason == "unrecognizable-stack-frame-layout"


def test_riscv_xlen_abi_conflict_and_nonstandard_extension_are_rejected():
    blob = _attributes(b"riscv", [(5, "rv64i2p1_zve32x1p0")])
    assert cpu_of(_image(243, True, blob)).reason == "unsupported-extension-context"
    assert cpu_of(_image(243, False, blob)).reason == "cpu-evidence-conflict"
    no_c = _attributes(b"riscv", [(5, "rv64i2p1_m2p0")])
    assert cpu_of(_image(243, True, no_c, flags=1)).reason == ("cpu-evidence-conflict")


def test_freertos_and_rtthread_arm_and_riscv_profile_selection():
    from core.unwind_meta import ElfSection
    from freertos.stack_frame_profile import (
        _arm_configuration_evidence,
    )
    from freertos.stack_frame_profile import (
        select_context as frt_select,
    )
    from rtthread.stack_frame_profile import select_context as rtt_select

    v7m_image = _image(40, False, _arm_file("cpu", 10, "M"))
    assert rtt_select(v7m_image).reason == "unverified-port"
    assert frt_select(v7m_image).reason == "unrecognizable-stack-frame-layout"

    anchored_v7m = ElfImage(
        v7m_image.pointer_size,
        v7m_image.endian,
        v7m_image.machine,
        v7m_image.sections,
        v7m_image.symbols,
        v7m_image.flags,
        (("PendSV_Handler", 0x100), ("pxPortInitialiseStack", 0x200)),
    )
    selected = frt_select(anchored_v7m)
    assert selected.reason == "ok" and selected.context is not None
    assert selected.convention is not None and selected.convention.sp_register == 13

    v8m_image = _image(40, False, _arm_file("cpu", 17, "M"))
    anchored_v8m = ElfImage(
        v8m_image.pointer_size,
        v8m_image.endian,
        v8m_image.machine,
        v8m_image.sections,
        v8m_image.symbols,
        v8m_image.flags,
        (("PendSV_Handler", 0x100), ("pxPortInitialiseStack", 0x200)),
    )
    assert frt_select(anchored_v8m, smp=True, macros={}).reason == (
        "port-configuration-unavailable"
    )
    assert (
        _arm_configuration_evidence(
            {
                "configENABLE_FPU": "1",
                "configENABLE_TRUSTZONE": "0",
                "configENABLE_MPU": "0",
            },
            mpu=False,
        )
        == "unsupported-extension-context"
    )
    assert (
        _arm_configuration_evidence(
            {
                "configENABLE_FPU": "0",
                "configENABLE_TRUSTZONE": "0",
                "configENABLE_MPU": "1",
                "configUSE_MPU_WRAPPERS_V1": "0",
            },
            mpu=True,
        )
        is None
    )
    rv_blob = _attributes(b"riscv", [(5, "rv32imac"), (4, 16)])
    rv_image = _image(243, False, rv_blob)
    assert frt_select(rv_image, smp=True).reason == "unsupported-extension-context"
    assert frt_select(rv_image).reason == "unrecognizable-stack-frame-layout"
    assert rtt_select(rv_image).reason == "unverified-port"
    _ = ElfSection


def test_riscv_route_and_initial_stack_helpers():
    from freertos.stack_frame_profile import (
        _riscv_initial_stack,
        _riscv_route_pc_store,
        _riscv_slots_include,
    )
    from tests.unit.core.test_stack_frame_parser import _i, _s

    assert _riscv_route_pc_store(
        {
            0x1000: ("addi", 6, 5, 4),
            0x1004: ("sw", 6, 2, 0),
        },
        mepc_register=5,
        addend=4,
    )
    assert _riscv_slots_include({1: {4, 8}}, {1: 4})
    assert not _riscv_slots_include({1: {4}}, {1: 8})

    text = b"".join(
        word.to_bytes(4, "little")
        for word in (
            _i(0x13, 10, 0, 10, -124),  # addi a0, a0, -124
            _s(0x23, 10, 11, 0, 2),  # sw a1, 0(a0)
            _s(0x23, 10, 12, 28, 2),  # sw a2, 28(a0)
            _s(0x23, 10, 0, 116, 2),  # sw zero, 116(a0)
            _i(0x73, 5, 2, 0, 0x300),  # csrr t0, mstatus
            _i(0x13, 5, 7, 5, 0x7FF),  # andi t0, t0, 0x7ff
            _i(0x13, 6, 0, 0, 1),  # addi t1, zero, 1
            _i(0x13, 6, 1, 6, 3),  # slli t1, t1, 3
            0x0062E2B3,  # or t0, t0, t1
            _s(0x23, 10, 5, 120, 2),  # sw t0, 120(a0)
            0x00000317,  # auipc t1, 0
            _i(0x03, 6, 2, 6, 0),  # lw t1, 0(t1)
            _s(0x23, 10, 6, 4, 2),  # sw t1, 4(a0)
            0x00008067,  # ret
        )
    )
    raw_elf = ElfImage.from_bytes(
        _elf(
            is_64=False,
            endian="little",
            machine=243,
            sections=[(".text", 0x1000, text, 1, 4)],
        )
    )
    assert raw_elf is not None
    anchored = ElfImage(
        raw_elf.pointer_size,
        raw_elf.endian,
        raw_elf.machine,
        raw_elf.sections,
        raw_elf.symbols,
        raw_elf.flags,
        (
            ("pxPortInitialiseStack", 0x1000),
            ("xTaskReturnAddress", 0x1028),
            ("xPortStartFirstTask", 0x1038),
        ),
    )
    initial = _riscv_initial_stack(anchored, 4)
    assert initial is not None
    size, stores = initial
    assert size == 124 and stores[0] == ("arg", 1)
    assert stores[4] == ("symbol", "xTaskReturnAddress")
    assert stores[120] == ("csr", 0x300)
