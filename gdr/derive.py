"""RTOS-neutral derived-value helpers shared by the adapter packages.

The adapters compute several derived diagnostics from raw kernel scalars.
The arithmetic is identical across RTOSes (wrap-safe timer expiry,
fill-byte watermarking, waiter-cell formatting); only the caller's
constants (tick width, whether ``overdue`` is meaningful, stack growth
direction) differ.  Keeping those pure functions here — with scalar inputs
and string/int outputs, never adapter model objects — preserves the
"adapter owns intermediate presentation models" decision while killing the
copy-paste between the adapters.
"""

from __future__ import annotations

from collections.abc import Sequence


def timer_expires_in(
    expiry: int | None,
    tick: int | None,
    tick_mask: int,
    *,
    in_overflow: bool = False,
    overdue: bool = True,
) -> str:
    """Render the wrap-safe remaining ticks until a timer expires.

    Current-list items carry the absolute expiry tick in the same epoch as
    the tick counter.  With ``overdue`` (the timer-list adapter) an item
    whose expiry already passed but is still linked means the daemon has not
    processed it yet, rendered as ``overdue`` instead of a huge unsigned wrap
    value; with ``overdue=False`` the wrapped unsigned subtraction stays the
    answer, matching ``(timeout - tick) & mask``.  Overflow-list items belong
    to the next tick epoch: their ``xItemValue`` wrapped, so the real expiry
    is ``2**bits + expiry``.
    """
    if expiry is None or tick is None:
        return "N/A"
    if in_overflow:
        return str(((tick_mask + 1) - tick) + expiry)
    if overdue and expiry < tick:
        return "overdue"
    return str((expiry - tick) & tick_mask)


def fill_watermark(
    stack: bytes | None,
    *,
    from_low: bool,
    word_bytes: int = 1,
    fill_byte: int = 0xA5,
) -> int | None:
    """Count untouched fill bytes at the low/high end of a stack window.

    Stacks are prefilled with a fill byte (0xa5 by default, configurable
    per adapter) under the watermarking macros;
    the high-water mark is the number of words never overwritten.  ``None``
    when the window is empty or its first byte is not the fill byte (the
    build never prefills, or the stack grew past its watermark).  The caller
    decides growth direction (``from_low``) and fill byte; only the count is
    shared.
    """
    if stack is None or not stack:
        return None
    marker = bytes([fill_byte])
    if from_low:
        if stack[0] != fill_byte:
            return None
        raw = len(stack.lstrip(marker))
    else:
        if stack[-1] != fill_byte:
            return None
        raw = len(stack.rstrip(marker))
    return (len(stack) - raw) // word_bytes


def waiter_cell(
    names: Sequence[str] | None,
    *,
    available: bool = True,
) -> str:
    """Render one waiter cell as ``count@name1,name2`` or ``N/A``.

    ``count`` always leads so truncation of long name lists drops *names*,
    never the diagnostic count; ``None`` or ``available=False`` renders the
    neutral ``N/A`` (a version without the wait list must not fabricate 0).
    """
    if not available or names is None:
        return "N/A"
    if not names:
        return "0"
    return f"{len(names)}@{','.join(names)}"
