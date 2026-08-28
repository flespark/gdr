"""Stream / message / batching buffer geometry and classification.

A ``StreamBufferDef_t`` is a byte ring addressed by three offsets
(``xHead``/``xTail``/``xTriggerLevelBytes``) plus a raw storage pointer
``pucBuffer``; there is no byte-count member, so bytes and space are derived
with the kernel's own arithmetic (stream_buffer.c ``prvBytesInBuffer`` /
the space computation in ``xStreamBufferSpacesAvailable``).  The trigger
comparison is deliberately asymmetric for batching buffers (``>`` vs the
usual ``>=``, stream_buffer.c ``prvBytesInBufferMeetTriggerLevel``).

Waiting writers/readers are stored as *single* ``TaskHandle_t`` fields
(``xTaskWaitingToSend``/``xTaskWaitingToReceive``), not lists -- so unlike
queues there is **no** waiter discovery channel for stream buffers, and an
unregistered dynamic buffer with no global handle can never be enumerated.

``sbBYTES_TO_STORE_MESSAGE_LENGTH`` (the size of the length prefix in a
message buffer) defaults to ``sizeof(size_t)``; the macro has no DWARF
representation, so the value is derived from ``size_t`` and always rendered
as an assumption, never as a DWARF fact.

Every GDB entry point goes through module-level helpers (``read_path``,
``read_int``, ...) so the unit tests can drive the logic without a GDB
session, following ``freertos/timers.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from freertos.layout import FreeRtosLayout
from freertos.navigation import source_label, task_name_at
from gdr.adapter_api import ObjectTable
from gdr.formatting import format_address
from gdr.gdb_bridge import get_arch_info, lookup_type, read_bytes, read_int
from gdr.layout import read_path

try:
    import gdb
except ImportError:
    gdb = None  # type: ignore[assignment]

if gdb is not None:
    _STREAM_ERRORS: tuple[type[BaseException], ...] = (
        gdb.error,
        gdb.MemoryError,
        IndexError,
        TypeError,
        ValueError,
        AttributeError,
    )
else:
    _STREAM_ERRORS = (IndexError, TypeError, ValueError, AttributeError)

# stream_buffer.c sbFLAGS_* (ucFlags bit definitions).
_FLAG_MESSAGE_BUFFER = 0x01
_FLAG_STATICALLY_ALLOCATED = 0x02
_FLAG_BATCHING_BUFFER = 0x04


def bytes_in_buffer(head: int, tail: int, length: int) -> int:
    """Return the number of bytes currently stored in a stream buffer.

    Mirrors stream_buffer.c ``prvBytesInBuffer``: ``count = xLength +
    xHead - xTail``, decremented by ``xLength`` when it reaches ``xLength``
    (the implicit two's-complement wrap of the raw ring offsets).  Exactly
    equivalent to ``(xHead - xTail) % xLength`` including the
    ``xHead < xTail`` wrap.
    """
    count = length + head - tail
    if count >= length:
        count -= length
    return count


def spaces_available(head: int, tail: int, length: int) -> int:
    """Return the bytes available for a write of a *single* byte stream.

    Mirrors the space computation in stream_buffer.c
    ``xStreamBufferSpacesAvailable``: ``xLength + xTail - xHead - 1``,
    minus ``xLength`` when it overflows.  One byte is always reserved so a
    full ring stays distinguishable from an empty one; the static variant's
    ``xLength`` equals the storage size while a dynamic buffer's ``xLength``
    already includes the extra byte, which is why the *capacity* of both is
    ``xLength - 1``.
    """
    space = length + tail - head - 1
    if space >= length:
        space -= length
    return space


def trigger_met(bytes_used: int, trigger: int | None, batching: bool) -> bool:
    """Whether the byte count crosses the trigger level.

    Batching buffers notify only on a *strictly greater* count
    (``>``), every other buffer on ``>=`` (stream_buffer.c
    ``prvBytesInBufferMeetTriggerLevel``).  The asymmetry matters exactly at
    ``bytes == trigger``: a batching buffer at the level still blocks its
    receiver.
    """
    if trigger is None:
        return False
    return bytes_used > trigger if batching else bytes_used >= trigger


def stream_kind(flags: int | None) -> str:
    """Classify a buffer by ``ucFlags``: ``stream``/``message``/``batching``.

    The static bit is deliberately left out of the kind: it stays a property
    of the object (Src / detail), never of the table's Type column, so the
    column contract does not drift between static-only and dynamic variants.
    """
    if flags is None:
        return "unknown"
    if flags & _FLAG_BATCHING_BUFFER:
        return "batching"
    if flags & _FLAG_MESSAGE_BUFFER:
        return "message"
    return "stream"


def stream_label(flags: int | None) -> str:
    """Render the full six-value type label, e.g. ``message+static``.

    ``ucFlags`` can be 0/1/2/3/4/6 (stream_buffer.c): the message bit (1),
    the static-allocation bit (2) and, from V11.1.0, the batching bit (4)
    combine freely.  This full label belongs in the per-object detail, where
    it cannot destabilize a variant-wide table assertion.
    """
    kind = stream_kind(flags)
    if flags is None or not flags & _FLAG_STATICALLY_ALLOCATED:
        return kind
    return f"{kind}+static"


def message_length_size() -> tuple[int, bool]:
    """Return ``(bytes, assumed)`` for ``sbBYTES_TO_STORE_MESSAGE_LENGTH``.

    The macro is ``sizeof(configMESSAGE_BUFFER_LENGTH_TYPE)`` which defaults
    to ``size_t`` (FreeRTOS.h), and the macro has no DWARF representation and
    the only kernel function carrying the typedef is usually not linked into
    the fixture -- so the value is derived from ``sizeof(size_t)`` and marked
    *assumed* so readers never mistake it for a probed fact.  The size_t path
    failing (no DWARF) degrades to a 4-byte assumption for the target byte
    order, still flagged assumed.
    """
    mb_type = lookup_type("configMESSAGE_BUFFER_LENGTH_TYPE")
    if mb_type is not None:
        try:
            return int(mb_type.sizeof), False
        except _STREAM_ERRORS:
            pass
    size_t = lookup_type("size_t")
    if size_t is not None:
        try:
            return int(size_t.sizeof), True
        except _STREAM_ERRORS:
            pass
    return 4, True


@dataclass
class FreeRtosStreamBufferObject:
    """Display model for one stream/message/batching buffer.

    ``kind`` is the base classification (stream|message|batching); the
    static-allocation bit is reflected through the discovery channel and the
    detail's ``StaticallyAllocated`` row, never through ``kind``.  ``deleted``
    is the post-``vStreamBufferDeleteStatic`` signature (``xLength == 0`` and
    ``pucBuffer == NULL``, stream_buffer.c); a deleted buffer must short-
    circuit every geometry read -- dividing by ``xLength`` would raise.
    """

    name: str = "-"
    address: int = 0
    kind: str = "stream"
    flags: int | None = None
    deleted: bool = False
    length: int | None = None
    capacity: int | None = None
    bytes_used: int | None = None
    space: int | None = None
    trigger: int | None = None
    trigger_met: bool | None = None
    next_message: int | None = None
    recv_waiter: str | None = None
    send_waiter: str | None = None
    notification_index: int | None = None
    message_length_bytes: int = 4
    message_length_assumed: bool = True
    source: str = ""
    extra_sources: tuple[str, ...] = ()


def value_to_stream_buffer_object(
    value,
    found,
    layout: FreeRtosLayout,
) -> FreeRtosStreamBufferObject:
    """Convert a discovered stream-buffer address into the display model.

    A deleted buffer (``xLength == 0 && pucBuffer == NULL``) stops here:
    capacity/bytes/space/trigger stay ``None`` and the detail refuses the
    deferred division instead of raising on a zero length.
    """
    obj = FreeRtosStreamBufferObject(
        name=found.name or "-",
        address=found.address,
        source=found.source,
        extra_sources=found.extra_sources,
    )
    if value is None:
        return obj
    length = read_int(read_path(value, ("xLength",)))
    buffer = read_int(read_path(value, ("pucBuffer",)))
    obj.length = length
    # Reason: the deleted signature is xLength==0 *and* a readable NULL
    # pucBuffer (vStreamBufferDeleteStatic memsets the struct); an unreadable
    # pointer is not evidence of deletion and must not claim it.
    obj.deleted = length == 0 and buffer == 0
    if obj.deleted:
        return obj
    head = read_int(read_path(value, ("xHead",)))
    tail = read_int(read_path(value, ("xTail",)))
    trigger = read_int(read_path(value, ("xTriggerLevelBytes",)))
    flags = read_int(read_path(value, ("ucFlags",)))
    obj.flags = flags
    obj.kind = stream_kind(flags)
    obj.capacity = length - 1 if length is not None and length > 0 else None
    if head is not None and tail is not None and length is not None and length > 0:
        obj.bytes_used = bytes_in_buffer(head, tail, length)
        obj.space = spaces_available(head, tail, length)
    obj.trigger = trigger
    if obj.bytes_used is not None and trigger is not None:
        obj.trigger_met = trigger_met(
            obj.bytes_used, trigger, batching=obj.kind == "batching"
        )
    obj.message_length_bytes, obj.message_length_assumed = message_length_size()
    if obj.kind == "message" and obj.bytes_used is not None:
        # Reason: NextMsg is the length prefix stored at ring offset xTail
        # (stream_buffer.c prvReadBytesFromBuffer reads pucBuffer[xTail]),
        # only valid while a full length field has been written
        # (prvBytesInBuffer > sbBYTES_TO_STORE_MESSAGE_LENGTH guard); the
        # prefix width is the message-length assumption, not a hard-coded 4.
        if obj.bytes_used > obj.message_length_bytes and tail is not None and buffer:
            obj.next_message = _read_prefix(buffer, tail, obj)
        else:
            obj.next_message = None
    recv = read_int(read_path(value, ("xTaskWaitingToReceive",)))
    send = read_int(read_path(value, ("xTaskWaitingToSend",)))
    if recv:
        obj.recv_waiter = task_name_at(recv, layout) or "-"
    if send:
        obj.send_waiter = task_name_at(send, layout) or "-"
    if layout.config.stream_buffer_notification_index:
        index = read_int(read_path(value, ("uxNotificationIndex",)))
        obj.notification_index = index
    return obj


def _read_prefix(buffer: int, tail: int, obj: FreeRtosStreamBufferObject) -> int | None:
    """Read the length-prefix word at ring offset ``xTail`` (NextMsg).

    ``xTail`` is a byte *offset* into ``pucBuffer`` (stream_buffer.c), so the
    word lives at ``pucBuffer + xTail``; like the kernel's own
    ``prvReadBytesFromBuffer`` the read splits into two segments when the
    prefix straddles the ring end.  ``None`` (rendered ``-``) when the read
    fails or the offset/width is not a sane ring geometry.
    """
    length = obj.length
    width = obj.message_length_bytes
    if not length or tail is None or tail >= length or width > length:
        return None
    raw = bytearray()
    offset = tail
    remaining = width
    while remaining > 0:
        chunk = min(remaining, length - offset)
        piece = read_bytes(buffer + offset, chunk)
        if piece is None:
            return None
        raw.extend(piece)
        remaining -= chunk
        # Reason: prvReadBytesFromBuffer wraps the tail back to offset 0 once
        # the segment reaches xLength (stream_buffer.c).
        offset = 0
    arch = get_arch_info()
    if arch is not None and arch.endian == "big":
        return int.from_bytes(raw, "big")
    return int.from_bytes(raw, "little")


def bounds_check(obj: FreeRtosStreamBufferObject, value) -> str:
    """Three-state ring-bounds verdict: ``ok`` / ``skipped: ...`` / ``fail: ...``.

    xHead/xTail are byte offsets into the storage window and must stay below
    xLength, and a non-deleted buffer must own a non-NULL pucBuffer
    (stream_buffer.c prvInitialiseNewStreamBuffer).  A deleted buffer is
    structurally not a ring anymore and reports ``skipped`` -- an inapplicable
    check is never disguised as a failure.
    """
    if obj.deleted:
        return "skipped: deleted buffer"
    length = obj.length
    head = read_int(read_path(value, ("xHead",)))
    tail = read_int(read_path(value, ("xTail",)))
    buffer = read_int(read_path(value, ("pucBuffer",)))
    if length is None or head is None or tail is None or buffer is None:
        return "skipped: unreadable"
    if not buffer:
        return "fail: pucBuffer is NULL on a live buffer"
    if head >= length or tail >= length:
        return "fail: xHead/xTail outside [0, xLength)"
    return "ok"


_STREAM_BUFFER_HEADERS = [
    "Name",
    "Type",
    "Bytes",
    "Space",
    "Capacity",
    "Trigger",
    "NextMsg",
    "RecvWait",
    "SendWait",
    "Src",
    "Addr",
]


def stream_buffer_table(
    objects: list[FreeRtosStreamBufferObject],
    layout: FreeRtosLayout,
) -> ObjectTable:
    """Build the ``frt streambuffers`` table with the verbatim contract.

    Messages state why the table cannot lie: an explicit capability note for
    builds without stream buffers, and (always) the fact that stream buffers
    store no waiter lists, so the waiter channel does not apply and an
    unregistered dynamic buffer with no global handle is never enumerable.
    """
    messages: list[str] = []
    if not layout.config.stream_buffers:
        messages.append("no stream buffers in this build (configUSE_STREAM_BUFFERS=0)")
    messages.append(
        "stream buffers keep single TaskHandle_t waiters, not wait lists: "
        "no waiter discovery channel exists, and unregistered dynamic "
        "buffers with no global handle are not enumerable"
    )
    rows = []
    for obj in objects:
        next_msg: str
        if obj.kind == "message":
            next_msg = "-" if obj.next_message is None else f"0x{obj.next_message:x}"
        else:
            next_msg = "N/A"
        rows.append(
            [
                obj.name,
                obj.kind,
                "-" if obj.bytes_used is None else str(obj.bytes_used),
                "-" if obj.space is None else str(obj.space),
                "-" if obj.capacity is None else str(obj.capacity),
                "-" if obj.trigger is None else str(obj.trigger),
                next_msg,
                obj.recv_waiter or "-",
                obj.send_waiter or "-",
                source_label(obj.source, obj.extra_sources),
                format_address(obj.address),
            ]
        )
    return ObjectTable(
        headers=_STREAM_BUFFER_HEADERS,
        rows=rows,
        messages=messages,
        elastic=("Name",),
    )
