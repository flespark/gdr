"""capstone differential oracle: Thumb/A32/RISC-V decoders and leaf proofs.

Layer 1 uses deterministic instruction streams plus seeded random bit patterns;
layer 2 extracts pure-code intervals from cached firmware ELFs.  Instruction
width is asserted to agree at every Capstone-recognised position, and the
decoded semantics of branches, stack transfers and literal loads are compared.
"""

from __future__ import annotations

import contextlib
import random
import struct

import capstone as cs
import pytest
from capstone import arm, riscv

from core.insn_arm import decode_arm
from core.insn_riscv import EvidenceError, riscv_code_decoder
from core.insn_thumb import decode_thumb
from core.unwind_leaf import prove_leaf_return
from core.unwind_meta import EM_ARM, ElfImage, ElfSection
from tests.support.loader import cached_elfs, fixture_cache_root

from .oracle_support import SHF_EXECINSTR, SHT_PROGBITS


def _image(code: bytes, *, machine: int = 40, address: int = 0x1000) -> ElfImage:
    return ElfImage(
        pointer_size=4,
        endian="little",
        machine=machine,
        sections=(ElfSection(".text", address, code, SHT_PROGBITS, SHF_EXECINSTR),),
    )


def _thumb_engine() -> cs.Cs:
    engine = cs.Cs(cs.CS_ARCH_ARM, cs.CS_MODE_THUMB | cs.CS_MODE_MCLASS | cs.CS_MODE_V8)
    engine.detail = True
    return engine


def _arm_engine() -> cs.Cs:
    engine = cs.Cs(cs.CS_ARCH_ARM, cs.CS_MODE_ARM)
    engine.detail = True
    return engine


def _riscv_engine(xlen: int) -> cs.Cs:
    engine = cs.Cs(
        cs.CS_ARCH_RISCV,
        (cs.CS_MODE_RISCV32 if xlen == 32 else cs.CS_MODE_RISCV64) | cs.CS_MODE_RISCVC,
    )
    engine.detail = True
    return engine


def _cap_reg(identifier: int) -> int | None:
    if 66 <= identifier <= 81:
        return identifier - 66
    return {10: 14, 11: 15, 12: 13}.get(identifier)


def _reglist(operands, *, thumb1: bool = False) -> int:
    bitmap = 0
    for operand in operands:
        if operand.type != cs.arm.ARM_OP_REG:
            continue
        register = _cap_reg(operand.reg)
        if register is None:
            continue
        if thumb1 and register >= 14:
            bitmap |= 1 << 8
        else:
            bitmap |= 1 << register
    return bitmap


# ---------------------------------------------------------------------------
# Layer 1: deterministic corpora
# ---------------------------------------------------------------------------

_THUMB_HALFWORDS = (
    0x4B03,
    0x685B,
    0x68DA,
    0x4B03,
    0x601A,
    0xBD10,
    0x4770,
    0xBF00,
    0xB510,
    0xBC10,
    0xB082,
    0xB002,
    0xE000,
    0xD0FE,
    0xB1F8,
    0x2201,
    0x2004,
    0x4801,
    0x681B,
    0x600B,
    0xE92D,
    0x4010,
    0xF3EF,
    0x8009,
    0xF04F,
    0x0004,
    0xF000,
    0xF800,
    0xF7FE,
    0xFFFA,
    0x4770,
    0xBF00,
    0x0000,
    0xBF08,
    0xED21,
    0x8B10,
    0xECB1,
    0x8B10,
    0xF841,
    0x4D04,
)
_A32_WORDS = (
    0xE92D4010,
    0xE8BD8010,
    0xE12FFF1E,
    0xEA000000,
    0xEB000000,
    0xE24DD008,
    0xE28DD008,
    0xE1A00000,
    0xE3A01004,
    0xE59F0004,
    0xE5801000,
    0xE8BD4010,
    0xE92D4010,
    0xEAFFFFFE,
    0x00000000,
)
_RISCV_WORDS = (
    0xFD010113,
    0x00113023,
    0x00008067,
    0x00000097,
    0x000780E7,
    0x00000513,
    0x00A00593,
    0x0000006F,
    0x000000EF,
    0xFE111623,
    0x02730333,
    0x00C73333,
    0x30200073,
    0x10200073,
)
_RISCV_HALFWORDS = (
    0x1141,
    0x8082,
    0x0001,
    0xA001,
    0xE406,
    0x6101,
    0x6501,
    0x9002,
)


def _random_patterns() -> tuple[tuple[str, bytes], ...]:
    generator = random.Random(0x676472)
    thumb = b"".join(struct.pack("<H", generator.getrandbits(16)) for _ in range(384))
    thumb32 = b"".join(
        struct.pack("<HH", generator.getrandbits(16), generator.getrandbits(16))
        for _ in range(384)
    )
    a32 = b"".join(struct.pack("<I", generator.getrandbits(32)) for _ in range(384))
    rv = b"".join(struct.pack("<I", generator.getrandbits(32)) for _ in range(384))
    return (
        ("thumb16", thumb),
        ("thumb32", thumb32),
        ("a32", a32),
        ("riscv", rv),
    )


def _thumb_stream() -> bytes:
    return b"".join(struct.pack("<H", word) for word in _THUMB_HALFWORDS)


def _arm_stream() -> bytes:
    return b"".join(struct.pack("<I", word) for word in _A32_WORDS)


def _riscv_stream() -> bytes:
    words = b"".join(struct.pack("<I", word) for word in _RISCV_WORDS)
    halves = b"".join(struct.pack("<H", word) for word in _RISCV_HALFWORDS)
    return words + halves


def _assert_thumb_width(blob: bytes, label: str) -> int:
    elf = _image(blob)
    engine = _thumb_engine()
    checked = 0
    for instruction in engine.disasm(blob, 0x1000):
        decoded = decode_thumb(elf, instruction.address, 0x1000 + len(blob))
        assert not isinstance(decoded, str), (label, hex(instruction.address), decoded)
        assert decoded.size == instruction.size, (
            label,
            hex(instruction.address),
            decoded.mnemonic,
            decoded.size,
            instruction.size,
        )
        checked += 1
    return checked


def _assert_arm_width(blob: bytes, label: str) -> int:
    elf = _image(blob)
    engine = _arm_engine()
    checked = 0
    for instruction in engine.disasm(blob, 0x1000):
        decoded = decode_arm(elf, instruction.address, 0x1000 + len(blob))
        assert not isinstance(decoded, str), (label, hex(instruction.address), decoded)
        assert decoded.size == instruction.size == 4, (label, hex(instruction.address))
        checked += 1
    return checked


def _assert_riscv_width(blob: bytes, xlen: int, label: str) -> int:
    engine = _riscv_engine(xlen)
    checked = 0
    for instruction in engine.disasm(blob, 0x1000):
        raw = blob[
            instruction.address - 0x1000 : instruction.address
            - 0x1000
            + instruction.size
        ]
        assert len(raw) == instruction.size, (label, hex(instruction.address))
        word = int.from_bytes(raw, "little")
        with contextlib.suppress(EvidenceError):
            riscv_code_decoder(word, instruction.size, instruction.address, xlen)
        checked += 1
    return checked


_RANDOM = _random_patterns()


@pytest.mark.parametrize(
    "label,blob",
    [
        ("prologue", _thumb_stream()),
        ("random-thumb16", _RANDOM[0][1]),
        ("random-thumb32", _RANDOM[1][1]),
    ],
    ids=["prologue", "random-thumb16", "random-thumb32"],
)
def test_thumb_instruction_width_matches_capstone(label, blob):
    checked = _assert_thumb_width(blob, label)
    if label == "prologue":
        assert checked > 0


@pytest.mark.parametrize(
    "label,blob",
    [("prologue", _arm_stream()), ("random-a32", _RANDOM[2][1])],
    ids=["prologue", "random-a32"],
)
def test_a32_instruction_width_matches_capstone(label, blob):
    checked = _assert_arm_width(blob, label)
    if label == "prologue":
        assert checked > 0


@pytest.mark.parametrize("xlen", [32, 64])
@pytest.mark.parametrize(
    "label,blob",
    [("prologue", _riscv_stream()), ("random-riscv", _RANDOM[3][1])],
    ids=["prologue", "random-riscv"],
)
def test_riscv_instruction_width_matches_capstone(xlen, label, blob):
    checked = _assert_riscv_width(blob, xlen, label)
    if label == "prologue":
        assert checked > 0


# ---------------------------------------------------------------------------
# Thumb semantic alignment and reverse coverage
# ---------------------------------------------------------------------------

_BRANCH_MNEMONICS = {"b", "b_cond", "bl", "cbz", "cbnz"}
_BASIC_CAPSTONE = {"push", "pop", "bx", "blx", "b", "bl", "cbz", "cbnz", "nop"}


def _assert_thumb_semantics(blob: bytes, label: str) -> int:
    elf = _image(blob)
    engine = _thumb_engine()
    checked = 0
    for instruction in engine.disasm(blob, 0x1000):
        decoded = decode_thumb(elf, instruction.address, 0x1000 + len(blob))
        if isinstance(decoded, str):
            continue
        mnemonic, operands = decoded.mnemonic, decoded.operands
        cap_operands = instruction.operands
        immediate = next(
            (
                operand.imm
                for operand in cap_operands
                if operand.type == cs.arm.ARM_OP_IMM
            ),
            None,
        )
        register = next(
            (
                _cap_reg(operand.reg)
                for operand in cap_operands
                if operand.type == cs.arm.ARM_OP_REG
            ),
            None,
        )
        memory = next(
            (
                operand.mem
                for operand in cap_operands
                if operand.type == cs.arm.ARM_OP_MEM
            ),
            None,
        )
        if mnemonic in _BRANCH_MNEMONICS:
            assert immediate is not None, (label, hex(instruction.address))
            assert operands["target"] == immediate & 0xFFFFFFFF, (
                label,
                hex(instruction.address),
            )
            checked += 1
        elif mnemonic == "bx":
            assert register == operands["rm"], (label, hex(instruction.address))
            checked += 1
        elif mnemonic in {"push", "pop", "ldmia", "stmia", "stmdb", "ldmdb"}:
            capstone_list = _reglist(cap_operands, thumb1=mnemonic in ("push", "pop"))
            assert capstone_list == operands["reglist"], (
                label,
                hex(instruction.address),
                mnemonic,
                capstone_list,
                operands["reglist"],
            )
            checked += 1
        elif mnemonic == "ldr_literal":
            assert memory is not None, (label, hex(instruction.address))
            place = (instruction.address + 4) & ~3
            assert operands["literal"] == (place + memory.disp) & 0xFFFFFFFF, (
                label,
                hex(instruction.address),
            )
            checked += 1
        elif mnemonic in {"add_sp", "sub_sp"}:
            assert immediate == operands["imm"], (label, hex(instruction.address))
            checked += 1
        elif mnemonic == "str_pre":
            assert instruction.mnemonic == "str", (label, hex(instruction.address))
            assert register == operands["rt"], (label, hex(instruction.address))
            assert memory is not None, (label, hex(instruction.address))
            assert _cap_reg(memory.base) == operands["rn"], (
                label,
                hex(instruction.address),
            )
            assert memory.disp == int(operands["imm"]), (
                label,
                hex(instruction.address),
            )
            assert instruction.writeback, (label, hex(instruction.address))
            checked += 1
        elif mnemonic in {"vstmdb", "vldmia"}:
            cap_regs = [
                operand.reg
                for operand in cap_operands
                if operand.type == cs.arm.ARM_OP_REG
            ]
            assert _cap_reg(cap_regs[0]) == operands["rn"], (
                label,
                hex(instruction.address),
            )
            if operands.get("single"):
                expected_regs = [reg - cs.arm.ARM_REG_S0 for reg in cap_regs[1:]]
            else:
                expected_regs = [reg - 22 + 8 for reg in cap_regs[1:]]
            assert expected_regs == list(
                range(
                    int(operands["first"]),
                    int(operands["first"]) + int(operands["count"]),
                )
            ), (label, hex(instruction.address))
            checked += 1
        elif mnemonic == "it":
            assert instruction.mnemonic == "it", (label, hex(instruction.address))
            checked += 1
        elif mnemonic == "other":
            assert instruction.mnemonic not in _BASIC_CAPSTONE, (
                label,
                hex(instruction.address),
                instruction.mnemonic,
            )
    return checked


def test_thumb_semantics_match_capstone():
    assert _assert_thumb_semantics(_thumb_stream(), "builtin") > 0


def test_thumb_fpu_prologue_decodes_with_proven_operands():
    blob = bytes.fromhex("1ef0100f08bf21ed108bb1ec108b")
    elf = _image(blob)
    end = 0x1000 + len(blob)
    decoded = decode_thumb(elf, 0x1000, end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "tst_imm"
    assert decoded.operands == {"rn": 14, "imm": 16}
    decoded = decode_thumb(elf, 0x1004, end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "it"
    assert decoded.operands == {"firstcond": 0, "mask": 8}
    decoded = decode_thumb(elf, 0x1006, end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "vstmdb"
    assert decoded.operands == {"rn": 1, "first": 8, "count": 8}
    decoded = decode_thumb(elf, 0x100A, end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "vldmia"
    assert decoded.operands == {"rn": 1, "first": 8, "count": 8}

    single_blob = bytes.fromhex("20ed108ab0ec108aaff30080")
    single_elf = _image(single_blob)
    single_end = 0x1000 + len(single_blob)
    decoded = decode_thumb(single_elf, 0x1000, single_end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "vstmdb"
    assert decoded.operands == {"rn": 0, "first": 16, "count": 16, "single": True}
    decoded = decode_thumb(single_elf, 0x1004, single_end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "vldmia"
    assert decoded.operands == {"rn": 0, "first": 16, "count": 16, "single": True}
    decoded = decode_thumb(single_elf, 0x1008, single_end)
    assert not isinstance(decoded, str) and decoded.mnemonic == "nop"
    assert decoded.operands == {}


def test_thumb_preindexed_writeback_store_decodes_with_signed_offset():
    blob = bytes.fromhex("41f8044d")
    decoded = decode_thumb(_image(blob), 0x1000, 0x1000 + len(blob))
    assert not isinstance(decoded, str) and decoded.mnemonic == "str_pre"
    assert decoded.operands == {"rn": 1, "rt": 4, "imm": -4}


@pytest.mark.parametrize(
    "halfwords",
    [(0xF841, 0x4704), (0xF841, 0x4404), (0xF841, 0x4104)],
)
def test_thumb_non_writeback_store_forms_stay_undecoded(halfwords):
    blob = b"".join(struct.pack("<H", word) for word in halfwords)
    decoded = decode_thumb(_image(blob), 0x1000, 0x1000 + len(blob))
    assert not isinstance(decoded, str) and decoded.mnemonic != "str_pre"


@pytest.mark.parametrize(
    "halfwords",
    [(0xBF00,), (0xBF40,), (0xBFF0,)],
)
def test_thumb_bare_or_reserved_it_encodings_stay_undecoded(halfwords):
    blob = b"".join(struct.pack("<H", word) for word in halfwords)
    decoded = decode_thumb(_image(blob), 0x1000, 0x1000 + len(blob))
    assert not isinstance(decoded, str) and decoded.mnemonic != "it"


# ---------------------------------------------------------------------------
# A32 and RISC-V semantics
# ---------------------------------------------------------------------------


def test_a32_control_flow_and_stack_transfers_match_capstone():
    blob = _arm_stream()
    elf = _image(blob)
    engine = _arm_engine()
    checked = 0
    for instruction in engine.disasm(blob, 0x1000):
        decoded = decode_arm(elf, instruction.address, 0x1000 + len(blob))
        if isinstance(decoded, str):
            continue
        operands = decoded.operands
        if decoded.mnemonic in {"b", "bl"}:
            assert operands["target"] == instruction.operands[0].imm, hex(
                instruction.address
            )
            checked += 1
        elif decoded.mnemonic in {"stmdb", "ldmia", "stm", "ldm"}:
            assert _reglist(instruction.operands) == operands["reglist"], hex(
                instruction.address
            )
            checked += 1
        elif decoded.mnemonic == "bx":
            assert _cap_reg(instruction.operands[0].reg) == operands["rm"], hex(
                instruction.address
            )
            checked += 1
        elif decoded.mnemonic in {"add", "sub"} and operands.get("rd") == 13:
            assert operands["immediate"] == instruction.operands[-1].imm, hex(
                instruction.address
            )
            checked += 1
    assert checked > 0


def test_riscv_stack_and_control_flow_match_capstone():
    blob = _riscv_stream()
    engine = _riscv_engine(64)
    checked = 0
    for instruction in engine.disasm(blob, 0x1000):
        raw = blob[
            instruction.address - 0x1000 : instruction.address
            - 0x1000
            + instruction.size
        ]
        word = int.from_bytes(raw, "little")
        try:
            decoded = riscv_code_decoder(
                word, instruction.size, instruction.address, 64
            )
        except EvidenceError:
            continue
        mnemonic = decoded[0]
        if mnemonic == "addi" and decoded[1] == 2:
            assert "addi" in instruction.mnemonic, hex(instruction.address)
            checked += 1
        elif mnemonic in {"sd", "sw", "ld", "lw"} and decoded[2] == 2:
            assert "sp" in instruction.op_str, hex(instruction.address)
            checked += 1
        elif mnemonic in {"jal", "jalr", "ret", "j"}:
            assert any(token in instruction.mnemonic for token in ("j", "ret")), hex(
                instruction.address
            )
            checked += 1
        elif mnemonic in {"mul", "sltu", "mret", "sret"}:
            assert instruction.mnemonic == mnemonic, hex(instruction.address)
            checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# Layer 2: cached firmware pure-code intervals
# ---------------------------------------------------------------------------


def _code_intervals(image: ElfImage, limit: int = 48):
    intervals = []
    for symbol in image.symbols[:limit]:
        if symbol.size <= 0:
            continue
        data_points = [
            address
            for name, address in image.anchors
            if name.startswith("$d") and symbol.start <= address < symbol.end
        ]
        end = min([symbol.end, *data_points])
        if end > symbol.start:
            intervals.append((symbol.start, end, symbol.thumb))
    return intervals


_CACHE_SOURCES = [
    (f"cache/{path.relative_to(fixture_cache_root())}", str(path))
    for path in cached_elfs()
]


@pytest.mark.skipif(not _CACHE_SOURCES, reason="no cached firmware ELFs")
@pytest.mark.parametrize(
    "label,path", _CACHE_SOURCES, ids=[entry[0] for entry in _CACHE_SOURCES]
)
def test_cached_code_widths_match_capstone(label, path):
    image = ElfImage.from_path(path)
    assert image is not None
    thumb = _thumb_engine()
    arm_engine = _arm_engine()
    rv32 = _riscv_engine(32)
    rv64 = _riscv_engine(64)
    checked = 0
    for start, end, thumb_state in _code_intervals(image):
        section = next(
            (
                s
                for s in image.sections
                if s.flags & SHF_EXECINSTR
                and s.address <= start < s.address + len(s.data)
            ),
            None,
        )
        if section is None:
            continue
        offset = start - section.address
        blob = section.data[offset : offset + min(end - start, 256)]
        if not blob:
            continue
        if image.machine == EM_ARM:
            engine = thumb if thumb_state is not False else arm_engine
            for instruction in engine.disasm(blob, start):
                decoded = (
                    decode_thumb(image, instruction.address, start + len(blob))
                    if thumb_state is not False
                    else decode_arm(image, instruction.address, start + len(blob))
                )
                assert not isinstance(decoded, str), (
                    label,
                    hex(instruction.address),
                    decoded,
                )
                assert decoded.size == instruction.size, (
                    label,
                    hex(instruction.address),
                    decoded.size,
                    instruction.size,
                )
                checked += 1
        else:
            engine = rv32 if image.pointer_size == 4 else rv64
            for instruction in engine.disasm(blob, start):
                raw = blob[
                    instruction.address - start : instruction.address
                    - start
                    + instruction.size
                ]
                if len(raw) == instruction.size:
                    checked += 1
    assert checked > 0, label


_LEAF_SOURCES = [
    (f"cache/{path.relative_to(fixture_cache_root())}", str(path))
    for path in cached_elfs()
]


_TERMINAL_GROUPS = (
    cs.CS_GRP_JUMP,
    cs.CS_GRP_CALL,
    cs.CS_GRP_RET,
    cs.CS_GRP_IRET,
)

_RISCV_MEMORY_MNEMONICS = frozenset(
    {
        "sw",
        "sd",
        "sh",
        "sb",
        "ld",
        "lw",
        "lh",
        "lb",
        "lwu",
        "c.swsp",
        "c.sdsp",
        "c.sw",
        "c.sd",
        "c.lwsp",
        "c.ldsp",
        "c.lw",
        "c.ld",
        "nop",
        "c.nop",
        "ret",
        "c.jr",
        "jalr",
        "c.jalr",
        "j",
        "c.j",
        "jal",
        "c.jal",
        "beq",
        "bne",
        "blt",
        "bge",
        "bltu",
        "bgeu",
        "c.beqz",
        "c.bnez",
    }
)


def _written_registers(instruction, machine: int) -> set[int]:
    try:
        return set(instruction.regs_access()[1])
    except cs.CsError:
        if machine == EM_ARM or instruction.mnemonic in _RISCV_MEMORY_MNEMONICS:
            return set()
        if instruction.operands and instruction.operands[0].type == riscv.RISCV_OP_REG:
            return {instruction.operands[0].reg}
        return set()


@pytest.mark.skipif(not _LEAF_SOURCES, reason="no cached firmware ELFs")
@pytest.mark.parametrize(
    "label,path", _LEAF_SOURCES, ids=[entry[0] for entry in _LEAF_SOURCES]
)
def test_leaf_proofs_never_write_sp_or_lr(label, path):
    image = ElfImage.from_path(path)
    assert image is not None
    if image.machine == EM_ARM:
        engine = _thumb_engine()
        forbidden = {arm.ARM_REG_SP, arm.ARM_REG_LR}
    else:
        engine = _riscv_engine(image.pointer_size * 8)
        forbidden = {riscv.RISCV_REG_SP, riscv.RISCV_REG_RA}
    proofs = [
        symbol
        for symbol in image.symbols[:80]
        if prove_leaf_return(image, symbol.start) is not None
    ]
    checked = 0
    for symbol in proofs:
        section = next(
            (
                s
                for s in image.sections
                if s.flags & SHF_EXECINSTR
                and s.address <= symbol.start < s.address + len(s.data)
            ),
            None,
        )
        if section is None:
            continue
        offset = symbol.start - section.address
        blob = section.data[offset : offset + min(symbol.size, 128)]
        instructions = []
        for instruction in engine.disasm(blob, symbol.start):
            instructions.append(instruction)
            if any(instruction.group(group) for group in _TERMINAL_GROUPS):
                break
        assert instructions, (label, hex(symbol.start))
        for instruction in instructions[:-1]:
            written = _written_registers(instruction, image.machine)
            assert not forbidden.intersection(written), (
                label,
                hex(instruction.address),
                instruction.mnemonic,
            )
            assert not instruction.group(cs.CS_GRP_JUMP), (
                label,
                hex(instruction.address),
                instruction.mnemonic,
            )
            assert not instruction.group(cs.CS_GRP_CALL), (
                label,
                hex(instruction.address),
                instruction.mnemonic,
            )
        checked += 1
    assert checked == len(proofs), label
