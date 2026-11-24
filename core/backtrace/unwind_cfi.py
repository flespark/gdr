"""Bounded DWARF call-frame tables, expressions and encoded pointers (pure)."""

from __future__ import annotations

import struct
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

from ..elf import (
    DW_EH_PE_ABSPTR,
    DW_EH_PE_ALIGNED,
    DW_EH_PE_DATAREL,
    DW_EH_PE_FUNCREL,
    DW_EH_PE_INDIRECT,
    DW_EH_PE_OMIT,
    DW_EH_PE_PCREL,
    DW_EH_PE_SDATA2,
    DW_EH_PE_SDATA4,
    DW_EH_PE_SDATA8,
    DW_EH_PE_SLEB128,
    DW_EH_PE_TEXTREL,
    DW_EH_PE_UDATA2,
    DW_EH_PE_UDATA4,
    DW_EH_PE_UDATA8,
    DW_EH_PE_ULEB128,
    MAX_CFI_EXPRESSION_BYTES,
    MAX_CFI_OPCODES,
    MAX_CIE_COUNT,
    MAX_FDE_COUNT,
    MAX_STATE_DEPTH,
    Endian,
    Reader,
    _read_sleb,
    _read_uleb,
)


@dataclass(frozen=True)
class CfaRule:
    """Canonical frame address as ``register + offset`` or an expression.

    Exactly one form is valid: a register/offset rule has ``expression`` set to
    ``None``; an expression rule has ``register`` set to ``None`` and
    ``offset`` set to zero. ``error`` marks an unusable parsed rule.
    """

    register: int | None = None
    offset: int = 0
    error: str | None = None
    expression: bytes | None = None


@dataclass(frozen=True)
class RegisterRule:
    """How one register is recovered in the caller's frame.

    ``expression`` computes the save address; ``val_expression`` computes the
    register value directly. Both are encoded DWARF expressions and carry
    their bytes in ``expression``.
    """

    kind: Literal[
        "offset",
        "val_offset",
        "same",
        "undefined",
        "register",
        "expression",
        "val_expression",
    ]
    value: int = 0
    register: int | None = None
    expression: bytes | None = None


@dataclass(frozen=True)
class EncodedPointerContext:
    """Explicit bases and reader for non-PC-relative EH pointer encodings.

    An absent field is unavailable, not a guessed zero. ``text_base`` is the
    start of ``.text``, ``data_base`` the EH data base (``.got`` or
    ``.eh_frame_hdr``) and ``function_base`` the start of the containing
    function. ``read`` resolves ``DW_EH_PE_indirect`` pointers.
    """

    data_base: int | None = None
    text_base: int | None = None
    function_base: int | None = None
    read: Reader | None = None


@dataclass(frozen=True)
class CfiRow:
    """One CFI row at a PC; ``error`` means the row is unsafe to use."""

    cfa: CfaRule = field(default_factory=CfaRule)
    registers: dict[int, RegisterRule] = field(default_factory=dict)
    error: str | None = None
    return_register: int | None = None
    signal_frame: bool = False


@dataclass(frozen=True)
class _Cie:
    code_alignment: int
    data_alignment: int
    return_register: int
    initial: CfiRow
    augmentation: str
    fde_encoding: int | None


@dataclass(frozen=True)
class _Fde:
    start: int
    end: int
    cie: _Cie
    instructions: bytes
    section_address: int
    error: str | None = None


@dataclass(frozen=True)
class EhFrameHdr:
    """Validated ``.eh_frame_hdr`` binary search index for one ``.eh_frame``."""

    hdr_address: int
    eh_frame_address: int | None
    eh_frame_ptr_encoding: int
    fde_count_encoding: int
    table_encoding: int
    fde_count: int
    starts: tuple[int, ...]
    fde_addresses: tuple[int, ...]


def _build_fdes_index(
    fdes: tuple[_Fde, ...],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    starts = tuple(fde.start for fde in fdes)
    running = 0
    max_ends: list[int] = []
    for fde in fdes:
        if fde.start > 0 and fde.end > running:
            running = fde.end
        max_ends.append(running)
    return starts, tuple(max_ends)


class CfiTable:
    """A bounded CIE/FDE table parsed from one CFI section."""

    def __init__(
        self,
        fdes: tuple[_Fde, ...],
        *,
        address_size: int,
        endian: Endian,
        error: str | None = None,
        eh_frame_hdr: EhFrameHdr | None = None,
        eh_frame_data: bytes = b"",
        eh_frame_address: int = 0,
        pointer_context: EncodedPointerContext | None = None,
        index_error: str | None = None,
    ) -> None:
        ordered = tuple(sorted(fdes, key=lambda item: item.start))
        self._fdes = ordered
        self._starts, self._prefix_max_end = _build_fdes_index(ordered)
        self._address_size = address_size
        self._endian: Endian = endian
        self.error = error
        self._index = eh_frame_hdr
        self._eh_frame_data = eh_frame_data
        self._eh_frame_address = eh_frame_address
        self._pointer_context = pointer_context
        self._index_error = index_error
        self._degraded = eh_frame_hdr is None
        self._cie_cache: dict[int, _Cie] = {}
        self._indexed_fdes: dict[int, _Fde] = {}

    @property
    def address_size(self) -> int:
        """DWARF address size of the section."""
        return self._address_size

    @property
    def endian(self) -> Endian:
        """Byte order of the section."""
        return self._endian

    @property
    def indexed(self) -> bool:
        """Whether ``.eh_frame_hdr`` binary-search dispatch is active."""
        return self._index is not None and not self._degraded

    @property
    def index_error(self) -> str | None:
        """Reason ``.eh_frame_hdr`` was rejected or degraded, if any."""
        return self._index_error

    def __bool__(self) -> bool:
        if self._index is not None and not self._degraded:
            return self._index.fde_count > 0
        return bool(self._fdes)

    def _degrade(self, reason: str) -> None:
        self._degraded = True
        if self._index_error is None:
            self._index_error = reason
        self._cie_cache.clear()
        self._indexed_fdes.clear()
        fdes, error = _parse_cfi_raw(
            self._eh_frame_data,
            section_address=self._eh_frame_address,
            address_size=self._address_size,
            endian=self._endian,
            is_eh_frame=True,
            pointer_context=self._pointer_context,
        )
        self._fdes = tuple(sorted(fdes, key=lambda item: item.start))
        self._starts, self._prefix_max_end = _build_fdes_index(self._fdes)
        self.error = error

    def _decode_indexed_cie(self, cie_offset: int, fde_offset: int) -> _Cie | None:
        cached = self._cie_cache.get(cie_offset)
        if cached is not None:
            return cached
        data = self._eh_frame_data
        order = "<" if self._endian == "little" else ">"
        if cie_offset < 0 or cie_offset + 4 > fde_offset:
            return None
        length = struct.unpack_from(f"{order}I", data, cie_offset)[0]
        cursor = cie_offset + 4
        offset_size = 4
        if length == 0xFFFFFFFF:
            if cursor + 8 > fde_offset:
                return None
            length = struct.unpack_from(f"{order}Q", data, cursor)[0]
            cursor += 8
            offset_size = 8
        unit_end = cursor + length
        if length == 0 or unit_end > fde_offset or cursor + offset_size > unit_end:
            return None
        identifier = int.from_bytes(
            data[cursor : cursor + offset_size], self._endian, signed=False
        )
        if identifier != 0:
            return None
        parsed = _parse_cie(
            data,
            cursor + offset_size,
            unit_end,
            order=order,
            address_size=self._address_size,
        )
        if isinstance(parsed, str):
            return None
        self._cie_cache[cie_offset] = parsed
        return parsed

    def _decode_indexed_fde(self, index: int) -> _Fde | None:
        cached = self._indexed_fdes.get(index)
        if cached is not None:
            return cached
        assert self._index is not None
        data = self._eh_frame_data
        order = "<" if self._endian == "little" else ">"
        fde_offset = self._index.fde_addresses[index] - self._eh_frame_address
        if fde_offset < 0 or fde_offset + 4 > len(data):
            self._degrade("eh-hdr-fde-out-of-range")
            return None
        length = struct.unpack_from(f"{order}I", data, fde_offset)[0]
        cursor = fde_offset + 4
        offset_size = 4
        if length == 0xFFFFFFFF:
            if cursor + 8 > len(data):
                self._degrade("cfi-truncated-dwarf64")
                return None
            length = struct.unpack_from(f"{order}Q", data, cursor)[0]
            cursor += 8
            offset_size = 8
        unit_end = cursor + length
        if (
            length == 0
            or unit_end < cursor
            or unit_end > len(data)
            or cursor + offset_size > unit_end
        ):
            self._degrade("cfi-truncated-unit")
            return None
        id_offset = cursor
        identifier = int.from_bytes(
            data[id_offset : id_offset + offset_size], self._endian, signed=False
        )
        if identifier == 0:
            self._degrade("eh-hdr-fde-is-cie")
            return None
        cie_offset = id_offset - identifier
        cie = self._decode_indexed_cie(cie_offset, fde_offset)
        if cie is None:
            self._degrade("cfi-cie-missing")
            return None
        parsed = _parse_fde(
            data,
            id_offset + offset_size,
            unit_end,
            cie=cie,
            order=order,
            address_size=self._address_size,
            section_address=self._eh_frame_address,
            is_eh_frame=True,
            opcode_budget=MAX_CFI_OPCODES,
            pointer_context=self._pointer_context,
        )
        if parsed is None:
            self._degrade("eh-hdr-fde-mismatch")
            return None
        if isinstance(parsed, str):
            self._degrade(parsed)
            return None
        fde, _used = parsed
        if fde.start != self._index.starts[index]:
            self._degrade("eh-hdr-fde-mismatch")
            return None
        self._indexed_fdes[index] = fde
        return fde

    def _row_for_fdes(self, pc: int) -> CfiRow | None:
        idx = bisect_right(self._starts, pc) - 1
        if idx < 0 or self._prefix_max_end[idx] <= pc:
            return None
        chosen: _Fde | None = None
        for pos in range(idx, -1, -1):
            if self._prefix_max_end[pos] <= pc:
                break
            fde = self._fdes[pos]
            if fde.start <= 0:
                continue
            if chosen is not None and fde.start < chosen.start:
                break
            if fde.start <= pc < fde.end:
                chosen = fde
        if chosen is None:
            return None
        return _execute_cfi(
            chosen,
            pc,
            address_size=self._address_size,
            endian=self._endian,
        )

    def row_for(self, pc: int) -> CfiRow | None:
        """Execute the most specific FDE covering *pc*, or ``None``.

        Linkers can leave zero-address FDEs for discarded sections in
        ``.debug_frame``.  They overlap real functions, so a lookup ignores
        them and, among valid overlaps, takes the entry with the greatest
        start address (the innermost function).
        """
        if self._index is not None and not self._degraded:
            starts = self._index.starts
            idx = bisect_right(starts, pc) - 1
            while idx >= 0 and starts[idx] > 0:
                fde = self._decode_indexed_fde(idx)
                if self._degraded:
                    return self._row_for_fdes(pc)
                if fde is not None and fde.start <= pc < fde.end:
                    # Preserve first-entry selection on duplicate identical start PCs.
                    chosen = fde
                    scan = idx - 1
                    while scan >= 0 and starts[scan] == fde.start:
                        alt = self._decode_indexed_fde(scan)
                        if self._degraded:
                            return self._row_for_fdes(pc)
                        if alt is not None and alt.start <= pc < alt.end:
                            chosen = alt
                        scan -= 1
                    return _execute_cfi(
                        chosen,
                        pc,
                        address_size=self._address_size,
                        endian=self._endian,
                    )
                if idx > 0 and starts[idx - 1] == starts[idx]:
                    idx -= 1
                    continue
                return None
            return None
        return self._row_for_fdes(pc)

    @classmethod
    def from_rows(
        cls,
        ranges: Sequence[tuple[int, int, CfiRow]],
        *,
        return_register: int = 14,
        address_size: int = 4,
        endian: Endian = "little",
    ) -> CfiTable:
        """Build a bounded table from pre-verified ``(start, end, row)`` spans."""
        fdes = tuple(
            _Fde(
                start=start,
                end=end,
                cie=_Cie(
                    code_alignment=1,
                    data_alignment=-4,
                    return_register=(
                        row.return_register
                        if row.return_register is not None
                        else return_register
                    ),
                    initial=row,
                    augmentation="S" if row.signal_frame else "",
                    fde_encoding=None,
                ),
                instructions=b"",
                section_address=0,
            )
            for start, end, row in ranges
            if 0 < start < end
        )
        return cls(fdes, address_size=address_size, endian=endian)


def _parse_cfi_raw(
    data: bytes,
    *,
    section_address: int,
    address_size: int,
    endian: Endian,
    is_eh_frame: bool,
    pointer_context: EncodedPointerContext | None = None,
) -> tuple[list[_Fde], str | None]:
    order = "<" if endian == "little" else ">"
    cies: dict[int, _Cie | str] = {}
    fdes: list[_Fde] = []
    index = 0
    error: str | None = None
    opcode_budget = MAX_CFI_OPCODES
    while index < len(data):
        if len(fdes) >= MAX_FDE_COUNT or len(cies) >= MAX_CIE_COUNT:
            error = "cfi-record-limit"
            break
        unit_start = index
        if index + 4 > len(data):
            error = "cfi-truncated-length"
            break
        length = struct.unpack_from(f"{order}I", data, index)[0]
        index += 4
        offset_size = 4
        if length == 0xFFFFFFFF:
            if index + 8 > len(data):
                error = "cfi-truncated-dwarf64"
                break
            length = struct.unpack_from(f"{order}Q", data, index)[0]
            index += 8
            offset_size = 8
        if length == 0:
            break
        unit_end = index + length
        if unit_end < index or unit_end > len(data):
            error = "cfi-truncated-unit"
            break
        if index + offset_size > unit_end:
            error = "cfi-truncated-id"
            break
        identifier = int.from_bytes(
            data[index : index + offset_size], endian, signed=False
        )
        id_offset = index
        index += offset_size
        if identifier == (0 if is_eh_frame else (1 << (8 * offset_size)) - 1):
            parsed = _parse_cie(
                data,
                index,
                unit_end,
                order=order,
                address_size=address_size,
            )
            cies[unit_start] = parsed
            if isinstance(parsed, str):
                error = error or parsed
            index = unit_end
            continue
        cie_offset = id_offset - identifier if is_eh_frame else identifier
        cie = cies.get(cie_offset)
        if cie is None or isinstance(cie, str):
            failure = cie if isinstance(cie, str) else "cfi-cie-missing"
            error = error or failure
            index = unit_end
            continue
        parsed_fde = _parse_fde(
            data,
            index,
            unit_end,
            cie=cie,
            order=order,
            address_size=address_size,
            section_address=section_address,
            is_eh_frame=is_eh_frame,
            opcode_budget=opcode_budget,
            pointer_context=pointer_context,
        )
        if isinstance(parsed_fde, str):
            error = error or parsed_fde
            index = unit_end
            continue
        if parsed_fde is None:
            index = unit_end
            continue
        fde, used = parsed_fde
        opcode_budget -= used
        if opcode_budget <= 0:
            error = "cfi-opcode-limit"
            break
        fdes.append(fde)
        index = unit_end
    fdes.sort(key=lambda item: item.start)
    return fdes, error


def parse_eh_frame_hdr(
    data: bytes,
    *,
    hdr_address: int,
    eh_frame_address: int,
    eh_frame_size: int,
    address_size: int,
    endian: Endian,
    pointer_context: EncodedPointerContext | None = None,
) -> EhFrameHdr | str:
    """Parse and validate a ``.eh_frame_hdr`` binary search index.

    Returns :class:`EhFrameHdr` on success or a machine-readable error code
    when the index is absent, truncated, unsorted or inconsistent with
    ``.eh_frame``.
    """
    if len(data) < 4:
        return "eh-hdr-truncated"
    version, eh_frame_ptr_enc, fde_count_enc, table_enc = data[:4]
    if version != 1:
        return "eh-hdr-version-unsupported"
    hdr_context = EncodedPointerContext(
        data_base=hdr_address,
        text_base=pointer_context.text_base if pointer_context else None,
        function_base=None,
        read=pointer_context.read if pointer_context else None,
    )
    if eh_frame_ptr_enc == DW_EH_PE_OMIT:
        eh_frame_ptr: int | None = None
        index = 4
    else:
        decoded_ptr = _decode_encoded_pointer(
            data,
            4,
            len(data),
            encoding=eh_frame_ptr_enc,
            address_size=address_size,
            endian=endian,
            section_address=hdr_address,
            context=hdr_context,
        )
        if decoded_ptr is None:
            return "eh-hdr-malformed"
        eh_frame_ptr, index = decoded_ptr
        if eh_frame_ptr != eh_frame_address:
            return "eh-hdr-pointer-mismatch"
    if DW_EH_PE_OMIT in (fde_count_enc, table_enc):
        return "eh-hdr-no-table"
    if fde_count_enc & 0xF0:
        return "eh-hdr-malformed"
    decoded_count = _decode_encoded_pointer(
        data,
        index,
        len(data),
        encoding=fde_count_enc,
        address_size=address_size,
        endian=endian,
        section_address=hdr_address,
    )
    if decoded_count is None:
        return "eh-hdr-malformed"
    fde_count, index = decoded_count
    if fde_count < 0 or fde_count > MAX_FDE_COUNT:
        return "eh-hdr-record-limit"

    address_limit = 1 << (address_size * 8)
    eh_end = eh_frame_address + eh_frame_size
    starts: list[int] = []
    fde_addresses: list[int] = []
    prev_loc: int | None = None
    fmt = table_enc & 0x0F
    application = table_enc & 0x70
    fixed_pack = {
        DW_EH_PE_ABSPTR: ("Q" if address_size == 8 else "I", address_size),
        DW_EH_PE_UDATA2: ("H", 2),
        DW_EH_PE_UDATA4: ("I", 4),
        DW_EH_PE_UDATA8: ("Q", 8),
        DW_EH_PE_SDATA2: ("h", 2),
        DW_EH_PE_SDATA4: ("i", 4),
        DW_EH_PE_SDATA8: ("q", 8),
    }.get(fmt)
    if (
        fixed_pack is not None
        and not table_enc & DW_EH_PE_INDIRECT
        and application in (0, DW_EH_PE_DATAREL)
    ):
        pack_char, width = fixed_pack
        entry_size = width * 2
        table_bytes = fde_count * entry_size
        if index + table_bytes > len(data):
            return "eh-hdr-truncated"
        base = hdr_address if application == DW_EH_PE_DATAREL else 0
        if not 0 <= base < address_limit:
            return "eh-hdr-malformed"
        order = "<" if endian == "little" else ">"
        for raw_loc, raw_fde in struct.iter_unpack(
            f"{order}{pack_char}{pack_char}",
            data[index : index + table_bytes],
        ):
            loc = base + raw_loc
            fde_addr = base + raw_fde
            if not (0 <= loc < address_limit and 0 <= fde_addr < address_limit):
                return "eh-hdr-malformed"
            if prev_loc is not None and loc < prev_loc:
                return "eh-hdr-unsorted"
            if not eh_frame_address <= fde_addr < eh_end:
                return "eh-hdr-fde-out-of-range"
            prev_loc = loc
            starts.append(loc)
            fde_addresses.append(fde_addr)
    else:
        cursor = index
        for _ in range(fde_count):
            dec_loc = _decode_encoded_pointer(
                data,
                cursor,
                len(data),
                encoding=table_enc,
                address_size=address_size,
                endian=endian,
                section_address=hdr_address,
                context=hdr_context,
            )
            if dec_loc is None:
                return "eh-hdr-truncated"
            loc, cursor = dec_loc
            dec_fde = _decode_encoded_pointer(
                data,
                cursor,
                len(data),
                encoding=table_enc,
                address_size=address_size,
                endian=endian,
                section_address=hdr_address,
                context=hdr_context,
            )
            if dec_fde is None:
                return "eh-hdr-truncated"
            fde_addr, cursor = dec_fde
            if prev_loc is not None and loc < prev_loc:
                return "eh-hdr-unsorted"
            if not eh_frame_address <= fde_addr < eh_end:
                return "eh-hdr-fde-out-of-range"
            prev_loc = loc
            starts.append(loc)
            fde_addresses.append(fde_addr)

    return EhFrameHdr(
        hdr_address=hdr_address,
        eh_frame_address=eh_frame_ptr,
        eh_frame_ptr_encoding=eh_frame_ptr_enc,
        fde_count_encoding=fde_count_enc,
        table_encoding=table_enc,
        fde_count=fde_count,
        starts=tuple(starts),
        fde_addresses=tuple(fde_addresses),
    )


def parse_cfi_section(
    data: bytes,
    *,
    section_address: int,
    address_size: int,
    endian: Endian,
    is_eh_frame: bool,
    pointer_context: EncodedPointerContext | None = None,
    eh_frame_hdr: tuple[int, bytes] | None = None,
    eh_frame_hdr_error: str | None = None,
) -> CfiTable:
    """Parse one ``.debug_frame``/``.eh_frame`` section.

    Known FDE ranges retain their CIE/rule failures. An error whose coverage
    cannot be decoded is retained on the table and forbids unproven fallback
    at PCs without a valid independent rule. ``pointer_context`` supplies
    explicit bases for non-PC-relative EH pointer encodings; absent bases make
    those FDEs unavailable. When ``is_eh_frame`` and a valid ``eh_frame_hdr``
    index are supplied, FDE lookups binary-search the index on demand and
    degrade safely to linear ``.eh_frame`` parsing on any index error.
    """
    index_error = eh_frame_hdr_error
    if is_eh_frame and eh_frame_hdr is not None and index_error is None and data:
        hdr_address, hdr_data = eh_frame_hdr
        parsed_hdr = parse_eh_frame_hdr(
            hdr_data,
            hdr_address=hdr_address,
            eh_frame_address=section_address,
            eh_frame_size=len(data),
            address_size=address_size,
            endian=endian,
            pointer_context=pointer_context,
        )
        if isinstance(parsed_hdr, EhFrameHdr):
            if parsed_hdr.fde_count > 0 and len(data) >= 4:
                return CfiTable(
                    (),
                    address_size=address_size,
                    endian=endian,
                    eh_frame_hdr=parsed_hdr,
                    eh_frame_data=data,
                    eh_frame_address=section_address,
                    pointer_context=pointer_context,
                )
            index_error = "eh-hdr-empty"
        else:
            index_error = parsed_hdr
    fdes, error = _parse_cfi_raw(
        data,
        section_address=section_address,
        address_size=address_size,
        endian=endian,
        is_eh_frame=is_eh_frame,
        pointer_context=pointer_context,
    )
    return CfiTable(
        tuple(fdes),
        address_size=address_size,
        endian=endian,
        error=error,
        index_error=index_error,
    )


def _parse_cie(
    data: bytes,
    index: int,
    end: int,
    *,
    order: str,
    address_size: int,
) -> _Cie | str:
    if index >= end:
        return "cfi-malformed-cie"
    byteorder = "little" if order == "<" else "big"
    version = data[index]
    index += 1
    aug_end = data.find(b"\x00", index, end)
    if aug_end < 0:
        return "cfi-malformed-cie"
    augmentation = data[index:aug_end].decode("ascii", errors="replace")
    index = aug_end + 1
    if version not in (1, 3, 4):
        return "cfi-cie-version-unsupported"
    if version == 4:
        if index + 2 > end or data[index] != address_size or data[index + 1] != 0:
            return "cfi-cie-address-size-unsupported"
        index += 2
    code = _read_uleb(data, index, end)
    if code is None:
        return "cfi-malformed-cie"
    code_alignment, index = code
    data_align = _read_sleb(data, index, end)
    if data_align is None:
        return "cfi-malformed-cie"
    data_alignment, index = data_align
    if version == 1:
        if index >= end:
            return "cfi-malformed-cie"
        return_register = data[index]
        index += 1
    else:
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return "cfi-malformed-cie"
        return_register, index = decoded
    fde_encoding: int | None = None
    if augmentation.startswith("z"):
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return "cfi-malformed-cie"
        aug_length, index = decoded
        aug_end_data = index + aug_length
        if aug_end_data > end:
            return "cfi-malformed-cie"
        cursor = index
        for char in augmentation[1:]:
            if char == "R":
                if cursor >= aug_end_data:
                    return "cfi-malformed-cie"
                fde_encoding = data[cursor]
                cursor += 1
            elif char == "L":
                if cursor >= aug_end_data:
                    return "cfi-malformed-cie"
                cursor += 1
            elif char == "P":
                if cursor >= aug_end_data:
                    return "cfi-malformed-cie"
                encoding = data[cursor]
                cursor += 1
                skipped = _encoded_pointer_skip(
                    data, cursor, encoding, aug_end_data, address_size=address_size
                )
                if skipped is None:
                    return "cfi-malformed-cie"
                cursor = skipped
            elif char in "SB":
                continue
            else:
                return "cfi-augmentation-unsupported"
        index = aug_end_data
    elif augmentation:
        return "cfi-augmentation-unsupported"
    initial = _run_cfi(
        data[index:end],
        CfiRow(cfa=CfaRule(register=0, offset=0)),
        location=0,
        target=None,
        code_alignment=code_alignment,
        data_alignment=data_alignment,
        address_size=address_size,
        endian=byteorder,
        cie_row=None,
        max_steps=MAX_CFI_OPCODES,
    )
    if isinstance(initial, str):
        initial = CfiRow(error=initial)
    return _Cie(
        code_alignment=code_alignment,
        data_alignment=data_alignment,
        return_register=return_register,
        initial=initial,
        augmentation=augmentation,
        fde_encoding=fde_encoding,
    )


def _encoded_pointer_skip(
    data: bytes, index: int, encoding: int, end: int, *, address_size: int = 4
) -> int | None:
    """Consume an encoded pointer without interpreting it; return a new index."""
    if encoding == DW_EH_PE_OMIT:
        return index
    fmt = encoding & 0x0F
    if fmt == DW_EH_PE_ULEB128:
        decoded = _read_uleb(data, index, end)
        return None if decoded is None else decoded[1]
    if fmt == DW_EH_PE_SLEB128:
        decoded = _read_sleb(data, index, end)
        return None if decoded is None else decoded[1]
    sizes = {
        DW_EH_PE_ABSPTR: address_size,
        DW_EH_PE_UDATA2: 2,
        DW_EH_PE_UDATA4: 4,
        DW_EH_PE_UDATA8: 8,
        DW_EH_PE_SDATA2: 2,
        DW_EH_PE_SDATA4: 4,
        DW_EH_PE_SDATA8: 8,
    }
    size = sizes.get(fmt)
    if size is None or index + size > end:
        return None
    return index + size


def _decode_encoded_pointer(
    data: bytes,
    index: int,
    end: int,
    *,
    encoding: int,
    address_size: int,
    endian: Endian,
    section_address: int,
    absolute_position: int | None = None,
    context: EncodedPointerContext | None = None,
) -> tuple[int, int] | None:
    """Decode one EH pointer with explicit non-PC-relative bases.

    ``pcrel`` uses the section address and pointer position. ``textrel``,
    ``datarel`` and ``funcrel`` require the corresponding base from *context*;
    a missing base or absent indirect reader rejects the pointer. No base is
    inferred from a different section or from zero. Signed encodings are
    sign-extended before the base is added; the base, the base-plus-offset
    result and an indirect read range must all stay inside the target address
    space, so a negative or wrapped target is rejected.
    """
    if encoding == DW_EH_PE_OMIT:
        return 0, index
    fmt = encoding & 0x0F
    application = encoding & 0x70
    if application == DW_EH_PE_ALIGNED:
        alignment = address_size
        index += (-(section_address + index)) % alignment
    pointer_position = index if absolute_position is None else absolute_position
    if fmt == DW_EH_PE_ABSPTR:
        size = address_size
        if index + size > end:
            return None
        unsigned = int.from_bytes(data[index : index + size], endian)
        index += size
        value = unsigned
    elif fmt in (DW_EH_PE_UDATA2, DW_EH_PE_UDATA4, DW_EH_PE_UDATA8):
        size = {DW_EH_PE_UDATA2: 2, DW_EH_PE_UDATA4: 4, DW_EH_PE_UDATA8: 8}[fmt]
        if index + size > end:
            return None
        value = int.from_bytes(data[index : index + size], endian)
        index += size
    elif fmt in (DW_EH_PE_SDATA2, DW_EH_PE_SDATA4, DW_EH_PE_SDATA8):
        size = {DW_EH_PE_SDATA2: 2, DW_EH_PE_SDATA4: 4, DW_EH_PE_SDATA8: 8}[fmt]
        if index + size > end:
            return None
        value = int.from_bytes(data[index : index + size], endian, signed=True)
        index += size
    elif fmt == DW_EH_PE_ULEB128:
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            return None
        value, index = decoded
    elif fmt == DW_EH_PE_SLEB128:
        decoded = _read_sleb(data, index, end)
        if decoded is None:
            return None
        value, index = decoded
    else:
        return None
    address_limit = 1 << (address_size * 8)
    base = 0
    if application == DW_EH_PE_PCREL:
        base = section_address + pointer_position
    elif application == DW_EH_PE_TEXTREL:
        if context is None or context.text_base is None:
            return None
        base = context.text_base
    elif application == DW_EH_PE_DATAREL:
        if context is None or context.data_base is None:
            return None
        base = context.data_base
    elif application == DW_EH_PE_FUNCREL:
        if context is None or context.function_base is None:
            return None
        base = context.function_base
    elif application not in (0, DW_EH_PE_ALIGNED):
        return None
    if not 0 <= base < address_limit:
        return None
    result = value + base
    if not 0 <= result < address_limit:
        return None
    if encoding & DW_EH_PE_INDIRECT:
        if context is None or context.read is None or result <= 0:
            return None
        if result >= address_limit or address_size > address_limit - result:
            return None
        pointer = context.read(result, address_size)
        if pointer is None or len(pointer) != address_size:
            return None
        result = int.from_bytes(pointer, endian)
        if not 0 < result < address_limit:
            return None
    return result, index


def _parse_fde(
    data: bytes,
    index: int,
    end: int,
    *,
    cie: _Cie,
    order: str,
    address_size: int,
    section_address: int,
    is_eh_frame: bool,
    opcode_budget: int,
    pointer_context: EncodedPointerContext | None = None,
) -> tuple[_Fde, int] | str | None:
    byteorder = "little" if order == "<" else "big"
    if is_eh_frame and cie.fde_encoding is not None:
        start_pos = index
        decoded = _decode_encoded_pointer(
            data,
            index,
            end,
            encoding=cie.fde_encoding,
            address_size=address_size,
            endian=byteorder,
            section_address=section_address,
            absolute_position=start_pos,
            context=pointer_context,
        )
        if decoded is None:
            return "cfi-fde-pointer-unavailable"
        start, index = decoded
        fmt = cie.fde_encoding & 0x0F
        size_map = {
            DW_EH_PE_ABSPTR: address_size,
            DW_EH_PE_UDATA2: 2,
            DW_EH_PE_UDATA4: 4,
            DW_EH_PE_UDATA8: 8,
            DW_EH_PE_SDATA2: 2,
            DW_EH_PE_SDATA4: 4,
            DW_EH_PE_SDATA8: 8,
        }
        size = size_map.get(fmt)
        if size is None or index + size > end:
            return "cfi-malformed-fde"
        length = int.from_bytes(data[index : index + size], byteorder)
        index += size
    else:
        if index + address_size * 2 > end:
            return "cfi-malformed-fde"
        start = int.from_bytes(data[index : index + address_size], byteorder)
        index += address_size
        length = int.from_bytes(data[index : index + address_size], byteorder)
        index += address_size
    if length == 0 or start == 0:
        return None
    address_limit = 1 << (address_size * 8)
    if start < 0 or start >= address_limit or length > address_limit - start:
        return "cfi-fde-range-invalid"
    failure = None
    if cie.augmentation.startswith("z"):
        decoded = _read_uleb(data, index, end)
        if decoded is None:
            failure = "cfi-malformed-fde"
        else:
            aug_length, index = decoded
            if index + aug_length > end:
                failure = "cfi-malformed-fde"
            index += aug_length
    instructions = data[index:end]
    used = min(len(instructions) + 1, opcode_budget)
    return (
        _Fde(
            start=start,
            end=start + length,
            cie=cie,
            instructions=instructions,
            section_address=section_address,
            error=failure,
        ),
        used,
    )


def _execute_cfi(
    fde: _Fde,
    pc: int,
    *,
    address_size: int,
    endian: Endian,
) -> CfiRow:
    if fde.error is not None or fde.cie.initial.error is not None:
        return CfiRow(
            error=fde.error or fde.cie.initial.error,
            return_register=fde.cie.return_register,
            signal_frame="S" in fde.cie.augmentation,
        )
    result = _run_cfi(
        fde.instructions,
        fde.cie.initial,
        location=fde.start,
        target=pc,
        code_alignment=fde.cie.code_alignment,
        data_alignment=fde.cie.data_alignment,
        address_size=address_size,
        endian=endian,
        cie_row=fde.cie.initial,
        max_steps=MAX_CFI_OPCODES,
    )
    if isinstance(result, str):
        result = CfiRow(error=result)
    return replace(
        result,
        return_register=fde.cie.return_register,
        signal_frame="S" in fde.cie.augmentation,
    )


def _read_expression_block(
    data: bytes, index: int, end: int
) -> tuple[bytes, int] | str:
    """Read a ULEB128-sized CFI expression block with bounded length."""
    decoded = _read_uleb(data, index, end)
    if decoded is None:
        return "cfi-truncated-uleb"
    length, index = decoded
    if length > MAX_CFI_EXPRESSION_BYTES:
        return "cfi-expression-too-long"
    block_end = index + length
    if block_end > end:
        return "cfi-truncated-expression"
    return data[index:block_end], block_end


def _run_cfi(
    instructions: bytes,
    start_row: CfiRow,
    *,
    location: int,
    target: int | None,
    code_alignment: int,
    data_alignment: int,
    address_size: int,
    endian: Endian,
    cie_row: CfiRow | None,
    max_steps: int,
) -> CfiRow | str:
    row = CfiRow(cfa=start_row.cfa, registers=dict(start_row.registers))
    index = 0
    steps = 0
    state_stack: list[CfiRow] = []
    while index < len(instructions):
        steps += 1
        if steps > max_steps:
            return "cfi-opcode-limit"
        opcode = instructions[index]
        index += 1
        high = opcode & 0xC0
        low = opcode & 0x3F
        if high == 0x40:
            if target is not None and location + low * code_alignment > target:
                break
            location += low * code_alignment
            continue
        if high == 0x80:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            offset, index = decoded
            row.registers[low] = RegisterRule(
                kind="offset", value=offset * data_alignment
            )
            continue
        if high == 0xC0:
            restored = cie_row.registers.get(low) if cie_row is not None else None
            if restored is None:
                row.registers.pop(low, None)
            else:
                row.registers[low] = restored
            continue
        if opcode == 0x00:
            continue
        if opcode == 0x01:
            if index + address_size > len(instructions):
                return "cfi-truncated-set-loc"
            location = int.from_bytes(
                instructions[index : index + address_size], endian
            )
            index += address_size
            continue
        if opcode == 0x02:
            if index >= len(instructions):
                return "cfi-truncated-advance"
            delta = instructions[index]
            index += 1
            if target is not None and location + delta * code_alignment > target:
                break
            location += delta * code_alignment
            continue
        if opcode == 0x03:
            if index + 2 > len(instructions):
                return "cfi-truncated-advance"
            delta = int.from_bytes(instructions[index : index + 2], endian)
            index += 2
            if target is not None and location + delta * code_alignment > target:
                break
            location += delta * code_alignment
            continue
        if opcode == 0x04:
            if index + 4 > len(instructions):
                return "cfi-truncated-advance"
            delta = int.from_bytes(instructions[index : index + 4], endian)
            index += 4
            if target is not None and location + delta * code_alignment > target:
                break
            location += delta * code_alignment
            continue
        if opcode == 0x0A:
            if len(state_stack) >= MAX_STATE_DEPTH:
                return "cfi-state-stack-limit"
            state_stack.append(CfiRow(cfa=row.cfa, registers=dict(row.registers)))
            continue
        if opcode == 0x0B:
            if not state_stack:
                return "cfi-state-stack-underflow"
            row = state_stack.pop()
            continue
        if opcode == 0x07:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            row.registers[reg] = RegisterRule(kind="undefined")
            continue
        if opcode == 0x08:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            row.registers[reg] = RegisterRule(kind="same")
            continue
        if opcode == 0x09:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            source = _read_uleb(instructions, index, len(instructions))
            if source is None:
                return "cfi-truncated-uleb"
            source_value, index = source
            row.registers[reg_value] = RegisterRule(
                kind="register", register=source_value
            )
            continue
        if opcode == 0x0C:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = _read_uleb(instructions, index, len(instructions))
            if offset is None:
                return "cfi-truncated-uleb"
            offset_value, index = offset
            row = CfiRow(
                cfa=CfaRule(register=reg_value, offset=offset_value),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x0D:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            if row.cfa.expression is not None or row.cfa.register is None:
                return "cfi-cfa-rule-invalid"
            reg, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=reg, offset=row.cfa.offset),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x0E:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            if row.cfa.expression is not None or row.cfa.register is None:
                return "cfi-cfa-rule-invalid"
            offset, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=row.cfa.register, offset=offset),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x0F:
            decoded = _read_expression_block(instructions, index, len(instructions))
            if isinstance(decoded, str):
                return decoded
            expression, index = decoded
            row = CfiRow(
                cfa=CfaRule(expression=expression),
                registers=dict(row.registers),
            )
            continue
        if opcode in (0x10, 0x16):
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            decoded = _read_expression_block(instructions, index, len(instructions))
            if isinstance(decoded, str):
                return decoded
            expression, index = decoded
            row.registers[reg_value] = RegisterRule(
                kind="expression" if opcode == 0x10 else "val_expression",
                expression=expression,
            )
            continue
        if opcode in (0x05, 0x11):
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = (_read_sleb if opcode == 0x11 else _read_uleb)(
                instructions, index, len(instructions)
            )
            if offset is None:
                return "cfi-truncated-sleb" if opcode == 0x11 else "cfi-truncated-uleb"
            offset_value, index = offset
            row.registers[reg_value] = RegisterRule(
                kind="offset", value=offset_value * data_alignment
            )
            continue
        if opcode == 0x12:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            decoded = _read_sleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-sleb"
            offset, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=reg, offset=offset * data_alignment),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x13:
            decoded = _read_sleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-sleb"
            if row.cfa.expression is not None or row.cfa.register is None:
                return "cfi-cfa-rule-invalid"
            offset, index = decoded
            row = CfiRow(
                cfa=CfaRule(register=row.cfa.register, offset=offset * data_alignment),
                registers=dict(row.registers),
            )
            continue
        if opcode == 0x06:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            reg, index = decoded
            restored = cie_row.registers.get(reg) if cie_row is not None else None
            if restored is None:
                row.registers.pop(reg, None)
            else:
                row.registers[reg] = restored
            continue
        if opcode in (0x14, 0x15):
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = (_read_sleb if opcode == 0x15 else _read_uleb)(
                instructions, index, len(instructions)
            )
            if offset is None:
                return "cfi-truncated-uleb"
            offset_value, index = offset
            row.registers[reg_value] = RegisterRule(
                kind="val_offset", value=offset_value * data_alignment
            )
            continue
        if opcode == 0x2E:
            decoded = _read_uleb(instructions, index, len(instructions))
            if decoded is None:
                return "cfi-truncated-uleb"
            _value, index = decoded
            continue
        if opcode == 0x2F:
            reg = _read_uleb(instructions, index, len(instructions))
            if reg is None:
                return "cfi-truncated-uleb"
            reg_value, index = reg
            offset = _read_uleb(instructions, index, len(instructions))
            if offset is None:
                return "cfi-truncated-uleb"
            offset_value, index = offset
            row.registers[reg_value] = RegisterRule(
                kind="offset", value=-(offset_value * data_alignment)
            )
            continue
        return "cfi-opcode-unsupported"
    if row.cfa.error is not None:
        return row.cfa.error
    if row.cfa.expression is not None:
        if row.cfa.register is not None or row.cfa.offset:
            return "cfi-cfa-rule-invalid"
        return row
    if row.cfa.register is None:
        return "cfi-no-cfa"
    return row
