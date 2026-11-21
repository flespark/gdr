"""Bounded DWARF call-frame expression evaluation (pure module).

The evaluator is shared by the CFI parser's consumers and has no GDB import.
Register-location descriptions (``DW_OP_reg*``/``DW_OP_regx``) are rejected
instead of being treated as integer values. ``DW_OP_stack_value`` is accepted
only as the final operation of a ``DW_CFA_val_expression`` result; address and
CFA contexts reject it rather than dereferencing an implicit location.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from ..elf import (
    MAX_CFI_EXPRESSION_BYTES,
    MAX_CFI_EXPRESSION_READ_BYTES,
    MAX_CFI_EXPRESSION_READS,
    MAX_CFI_EXPRESSION_STACK,
    MAX_CFI_EXPRESSION_STEPS,
    Endian,
    Reader,
    _read_sleb,
    _read_uleb,
)

ExpressionPurpose = Literal["cfa", "address", "value"]

_FORBIDDEN_CFI_EXPRESSION_OPCODES = frozenset(
    {
        0x97,
        0x98,
        0x99,
        0x9A,
        0x9C,
        0xA1,
        0xA2,
        0xA4,
        0xA5,
        0xA6,
        0xA8,
        0xA9,
    }
)


def _instruction_end(data: bytes, index: int, *, address_size: int) -> int | str:
    """Return one instruction boundary after *index*, or a stop reason.

    This is a linear decoder for boundary validation, not an executor. It uses
    the same supported/forbidden classification as execution and rejects an
    unknown operator instead of treating its following bytes as instructions.
    """
    opcode = data[index]
    cursor = index + 1
    if opcode in _FORBIDDEN_CFI_EXPRESSION_OPCODES:
        return "cfi-expression-forbidden"
    if 0x50 <= opcode <= 0x6F or opcode == 0x90:
        return "cfi-expression-register-location"
    if 0x70 <= opcode <= 0x8F:
        decoded = _read_sleb(data, cursor, len(data))
        return "cfi-expression-truncated" if decoded is None else decoded[1]
    if opcode == 0x03:
        return (
            cursor + address_size
            if cursor + address_size <= len(data)
            else "cfi-expression-truncated"
        )
    if opcode == 0x15:
        return cursor + 1 if cursor + 1 <= len(data) else "cfi-expression-truncated"
    if opcode in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F):
        size = {
            0x08: 1,
            0x09: 1,
            0x0A: 2,
            0x0B: 2,
            0x0C: 4,
            0x0D: 4,
            0x0E: 8,
            0x0F: 8,
        }[opcode]
        return (
            cursor + size if cursor + size <= len(data) else "cfi-expression-truncated"
        )
    if opcode in (0x10, 0x23, 0x93):
        if opcode == 0x93:
            return "cfi-expression-unsupported"
        decoded = _read_uleb(data, cursor, len(data))
        return "cfi-expression-truncated" if decoded is None else decoded[1]
    if opcode == 0x11:
        decoded = _read_sleb(data, cursor, len(data))
        return "cfi-expression-truncated" if decoded is None else decoded[1]
    if opcode == 0x91:
        return "cfi-expression-unsupported"
    if opcode == 0x92:
        decoded = _read_uleb(data, cursor, len(data))
        if decoded is None:
            return "cfi-expression-truncated"
        decoded = _read_sleb(data, decoded[1], len(data))
        return "cfi-expression-truncated" if decoded is None else decoded[1]
    if opcode in (0x28, 0x2F):
        return cursor + 2 if cursor + 2 <= len(data) else "cfi-expression-truncated"
    if opcode == 0x94:
        return cursor + 1 if cursor + 1 <= len(data) else "cfi-expression-truncated"
    if opcode == 0x95:
        return "cfi-expression-unsupported"
    if opcode in (
        0x06,
        0x12,
        0x13,
        0x14,
        0x16,
        0x17,
        0x19,
        0x1A,
        0x1B,
        0x1C,
        0x1D,
        0x1E,
        0x1F,
        0x20,
        0x21,
        0x22,
        0x24,
        0x25,
        0x26,
        0x27,
        0x29,
        0x2A,
        0x2B,
        0x2C,
        0x2D,
        0x2E,
        0x96,
        0x9F,
    ):
        return cursor
    if 0x30 <= opcode <= 0x4F:
        return cursor
    if opcode == 0x18 or opcode in (0x9B, 0x9D, 0x9E, 0xA0, 0xA3, 0xA7):
        return "cfi-expression-unsupported"
    return "cfi-expression-unsupported"


def _expression_boundaries(expression: bytes, *, address_size: int) -> set[int] | str:
    """Return every linear instruction start and the expression end.

    ``DW_OP_stack_value`` must be the final bytecode operation; a non-final
    occurrence rejects the expression instead of exposing bytes after it.
    """
    boundaries = {0}
    index = 0
    while index < len(expression):
        next_index = _instruction_end(expression, index, address_size=address_size)
        if isinstance(next_index, str):
            return next_index
        if next_index <= index:
            return "cfi-expression-unsupported"
        if expression[index] == 0x9F and next_index != len(expression):
            return "cfi-expression-stack-value-not-terminal"
        boundaries.add(next_index)
        index = next_index
    boundaries.add(len(expression))
    return boundaries


def evaluate_cfi_expression(
    expression: bytes,
    *,
    registers: Mapping[int, int],
    initial_stack: tuple[int, ...] = (),
    read: Reader | None,
    address_size: int,
    endian: Endian,
    purpose: ExpressionPurpose = "value",
    stack_low: int | None = None,
    stack_high: int | None = None,
    max_steps: int | None = None,
    max_stack: int | None = None,
    max_reads: int | None = None,
    max_read_bytes: int | None = None,
    register_limit: int | None = None,
    register_failure: Callable[[int], str] | None = None,
) -> int | str:
    """Evaluate one bounded call-frame expression to its top stack value.

    The evaluator implements the integer subset of DWARF 5 section 2.5.1:
    literals and constants, ``breg``/``bregx`` register reads, stack shuffles,
    integer arithmetic and bit operations, signed comparisons, ``DW_OP_deref``
    and ``DW_OP_deref_size``, ``DW_OP_bra``/``DW_OP_skip`` and ``DW_OP_nop``.
    Values carry no explicit type, so generic arithmetic is unsigned modulo the
    target address width; ``DW_OP_div`` is the signed exception and
    ``DW_OP_mod`` uses unsigned operands. ``DW_OP_addr`` is an expression-local
    address constant, not a target read. CFA-relative rules pass the current CFA
    as ``initial_stack``; a CFA expression passes none, matching
    ``DW_CFA_def_cfa_expression``.

    ``DW_OP_reg*`` and ``DW_OP_regx`` are register location descriptions and
    are rejected; reading a register value requires ``breg``/``bregx``.
    ``DW_OP_stack_value`` is accepted only as the final operation for
    ``purpose="value"`` (a ``DW_CFA_val_expression`` result); address and CFA
    contexts reject it so a driver cannot dereference an implicit location as
    an address. A non-final ``DW_OP_stack_value`` rejects the expression.

    Branch targets must equal a linear instruction boundary or the expression
    end; a conditional branch target is validated whether or not its condition
    is taken. Arithmetic is modulo the target address width; signed operations
    use two's-complement interpretation of that width. Every target read is
    bounded by the expression budgets, the optional stack span and the target
    address space. Expression bytecode, stack, step, read-count and read-byte
    budgets are independent of the driver's ``MAX_FRAME_READ``. A failed
    evaluation returns a ``cfi-expression-*`` reason instead of a value.

    Args:
        expression: Raw expression bytes from a CFI block operand.
        registers: DWARF register numbers mapped to available integer values.
        initial_stack: Values pushed before execution (the current CFA for
            ``DW_CFA_expression`` and ``DW_CFA_val_expression``).
        read: Bounded target-memory reader; required only for dereferences.
        address_size: Target address width in bytes (4 or 8).
        endian: Target byte order.
        purpose: ``cfa``/``address`` for location results, or ``value`` for a
            ``DW_CFA_val_expression`` direct value.
        stack_low: Lowest readable byte, or ``None`` when unknown.
        stack_high: One past the highest readable byte, or ``None``.
        max_steps: Instruction budget; defaults to the module constant.
        max_stack: Evaluation-stack limit; defaults to the module constant.
        max_reads: Dereference count budget; defaults to the module constant.
        max_read_bytes: Total dereference byte budget; defaults to the module
            constant.
        register_limit: Exclusive upper bound for architectural register
            numbers, or ``None`` when the caller has no table width.
        register_failure: Optional source-specific reason for a missing
            register, preserving an earlier invalidation cause.

    Returns:
        The top stack value after execution, or a ``cfi-expression-*`` stop
        reason.
    """
    if address_size not in (4, 8):
        return "cfi-expression-address-size"
    if len(expression) > MAX_CFI_EXPRESSION_BYTES:
        return "cfi-expression-too-long"
    boundaries = _expression_boundaries(expression, address_size=address_size)
    if isinstance(boundaries, str):
        return boundaries
    step_limit = MAX_CFI_EXPRESSION_STEPS if max_steps is None else max_steps
    stack_limit = MAX_CFI_EXPRESSION_STACK if max_stack is None else max_stack
    read_limit = MAX_CFI_EXPRESSION_READS if max_reads is None else max_reads
    byte_limit = (
        MAX_CFI_EXPRESSION_READ_BYTES if max_read_bytes is None else max_read_bytes
    )
    width = address_size * 8
    mask = (1 << width) - 1
    address_limit = 1 << width
    stack = [value & mask for value in initial_stack]
    if len(stack) > stack_limit:
        return "cfi-expression-stack-limit"
    index = 0
    steps = 0
    reads = 0
    read_bytes = 0

    def push(value: int) -> bool:
        if len(stack) >= stack_limit:
            return False
        stack.append(value & mask)
        return True

    def pop() -> int | None:
        return stack.pop() if stack else None

    def pop_two() -> tuple[int, int] | None:
        if len(stack) < 2:
            return None
        top = stack.pop()
        second = stack.pop()
        return second, top

    def signed(value: int) -> int:
        return value - (1 << width) if value & (1 << (width - 1)) else value

    def take(size: int) -> bytes | None:
        nonlocal index
        if size < 0 or index + size > len(expression):
            return None
        raw = expression[index : index + size]
        index += size
        return raw

    def take_int(size: int, *, is_signed: bool = False) -> int | None:
        raw = take(size)
        return None if raw is None else int.from_bytes(raw, endian, signed=is_signed)

    def take_uleb() -> int | None:
        nonlocal index
        decoded = _read_uleb(expression, index, len(expression))
        if decoded is None:
            return None
        value, index = decoded
        return value

    def take_sleb() -> int | None:
        nonlocal index
        decoded = _read_sleb(expression, index, len(expression))
        if decoded is None:
            return None
        value, index = decoded
        return value

    def register_value(register: int) -> int | str:
        if register_limit is not None and not 0 <= register < register_limit:
            return "cfi-expression-register-unsupported"
        value = registers.get(register)
        if value is None:
            if register_failure is not None:
                return register_failure(register)
            return "cfi-expression-register-missing"
        return value & mask

    def read_at(address: int, size: int) -> int | str:
        nonlocal reads, read_bytes
        if read is None or address <= 0 or not 0 < size <= 8:
            return "cfi-expression-read-error"
        if address >= address_limit or size > address_limit - address:
            return "cfi-expression-read-error"
        if stack_low is not None and address < stack_low:
            return "cfi-expression-read-error"
        if stack_high is not None and address + size > stack_high:
            return "cfi-expression-read-error"
        if reads >= read_limit or read_bytes + size > byte_limit:
            return "cfi-expression-read-limit"
        reads += 1
        read_bytes += size
        raw = read(address, size)
        if raw is None or len(raw) != size:
            return "cfi-expression-read-error"
        return int.from_bytes(raw, endian)

    while index < len(expression):
        steps += 1
        if steps > step_limit:
            return "cfi-expression-step-limit"
        opcode = expression[index]
        index += 1
        if opcode in _FORBIDDEN_CFI_EXPRESSION_OPCODES:
            return "cfi-expression-forbidden"
        if 0x30 <= opcode <= 0x4F:
            if not push(opcode - 0x30):
                return "cfi-expression-stack-limit"
            continue
        if 0x50 <= opcode <= 0x6F or opcode == 0x90:
            return "cfi-expression-register-location"
        if 0x70 <= opcode <= 0x8F:
            offset = take_sleb()
            if offset is None:
                return "cfi-expression-truncated"
            value = register_value(opcode - 0x70)
            if isinstance(value, str):
                return value
            if not push(value + offset):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x03:
            value = take_int(address_size)
            if value is None:
                return "cfi-expression-truncated"
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x06:
            address = pop()
            if address is None:
                return "cfi-expression-stack-underflow"
            value = read_at(address, address_size)
            if isinstance(value, str):
                return value
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F):
            size = {
                0x08: 1,
                0x09: 1,
                0x0A: 2,
                0x0B: 2,
                0x0C: 4,
                0x0D: 4,
                0x0E: 8,
                0x0F: 8,
            }[opcode]
            value = take_int(size, is_signed=opcode in (0x09, 0x0B, 0x0D, 0x0F))
            if value is None:
                return "cfi-expression-truncated"
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x10:
            value = take_uleb()
            if value is None:
                return "cfi-expression-truncated"
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x11:
            value = take_sleb()
            if value is None:
                return "cfi-expression-truncated"
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x12:
            if not stack:
                return "cfi-expression-stack-underflow"
            if not push(stack[-1]):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x13:
            if pop() is None:
                return "cfi-expression-stack-underflow"
            continue
        if opcode == 0x14:
            if len(stack) < 2:
                return "cfi-expression-stack-underflow"
            if not push(stack[-2]):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x15:
            position = take_int(1)
            if position is None:
                return "cfi-expression-truncated"
            if position >= len(stack):
                return "cfi-expression-stack-underflow"
            if not push(stack[-1 - position]):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x16:
            if len(stack) < 2:
                return "cfi-expression-stack-underflow"
            stack[-1], stack[-2] = stack[-2], stack[-1]
            continue
        if opcode == 0x17:
            if len(stack) < 3:
                return "cfi-expression-stack-underflow"
            stack[-3:] = (stack[-1], stack[-3], stack[-2])
            continue
        if opcode == 0x18:
            return "cfi-expression-unsupported"
        if opcode == 0x19:
            value = pop()
            if value is None:
                return "cfi-expression-stack-underflow"
            value = signed(value)
            if value == -(1 << (width - 1)):
                return "cfi-expression-overflow"
            if not push(abs(value)):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x1A, 0x1C, 0x1E, 0x21, 0x22, 0x27):
            operands = pop_two()
            if operands is None:
                return "cfi-expression-stack-underflow"
            left, right = operands
            value = {
                0x1A: left & right,
                0x1C: left - right,
                0x1E: left * right,
                0x21: left | right,
                0x22: left + right,
                0x27: left ^ right,
            }[opcode]
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x1B, 0x1D):
            operands = pop_two()
            if operands is None:
                return "cfi-expression-stack-underflow"
            left, right = operands
            if right == 0:
                return "cfi-expression-divide-by-zero"
            if opcode == 0x1B:
                numerator = signed(left)
                denominator = signed(right)
                value = abs(numerator) // abs(denominator)
                if (numerator < 0) != (denominator < 0):
                    value = -value
            else:
                value = left % right
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x1F:
            value = pop()
            if value is None:
                return "cfi-expression-stack-underflow"
            if not push(-signed(value)):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x20:
            value = pop()
            if value is None:
                return "cfi-expression-stack-underflow"
            if not push(~value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x23:
            addend = take_uleb()
            if addend is None:
                return "cfi-expression-truncated"
            value = pop()
            if value is None:
                return "cfi-expression-stack-underflow"
            if not push(value + addend):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x24, 0x25, 0x26):
            operands = pop_two()
            if operands is None:
                return "cfi-expression-stack-underflow"
            value, shift = operands
            if shift >= width:
                return "cfi-expression-invalid-shift"
            if opcode == 0x24:
                result = value << shift
            elif opcode == 0x25:
                result = value >> shift
            else:
                result = signed(value) >> shift
            if not push(result):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2E):
            operands = pop_two()
            if operands is None:
                return "cfi-expression-stack-underflow"
            left, right = operands
            if opcode == 0x29:
                result = left == right
            elif opcode == 0x2A:
                result = signed(left) >= signed(right)
            elif opcode == 0x2B:
                result = signed(left) > signed(right)
            elif opcode == 0x2C:
                result = signed(left) <= signed(right)
            elif opcode == 0x2D:
                result = signed(left) < signed(right)
            else:
                result = left != right
            if not push(1 if result else 0):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x28, 0x2F):
            offset = take_int(2, is_signed=True)
            if offset is None:
                return "cfi-expression-truncated"
            target = index + offset
            if target not in boundaries:
                return "cfi-expression-branch-invalid"
            if opcode == 0x28:
                condition = pop()
                if condition is None:
                    return "cfi-expression-stack-underflow"
                if condition == 0:
                    continue
            index = target
            continue
        if opcode == 0x91:
            return "cfi-expression-unsupported"
        if opcode == 0x92:
            register = take_uleb()
            if register is None:
                return "cfi-expression-truncated"
            offset = take_sleb()
            if offset is None:
                return "cfi-expression-truncated"
            value = register_value(register)
            if isinstance(value, str):
                return value
            if not push(value + offset):
                return "cfi-expression-stack-limit"
            continue
        if opcode in (0x93, 0x95, 0x9B, 0x9D, 0x9E, 0xA0, 0xA3, 0xA7):
            return "cfi-expression-unsupported"
        if opcode == 0x94:
            size = take_int(1)
            if size is None:
                return "cfi-expression-truncated"
            if size not in (1, 2, 4, 8) or size > address_size:
                return "cfi-expression-unsupported"
            address = pop()
            if address is None:
                return "cfi-expression-stack-underflow"
            value = read_at(address, size)
            if isinstance(value, str):
                return value
            if not push(value):
                return "cfi-expression-stack-limit"
            continue
        if opcode == 0x96:
            continue
        if opcode == 0x9F:
            if purpose != "value":
                return "cfi-expression-stack-value-location"
            break
        return "cfi-expression-unsupported"
    if not stack:
        return "cfi-expression-invalid-result"
    return stack[-1]
