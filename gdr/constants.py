"""Shared limits and presentation defaults for the RTOS-neutral core.

All constants are module-level ``GDR_*`` names so a bare ``from
gdr.constants import *`` cannot collide with target or adapter names.
"""

# Master switch for verbose diagnostics.  When ``True``, an unforeseen error
# prints a full :func:`gdr.gdb_bridge.show_last_exception` diagnostic
# (exception banner, framed stacktrace, GDB/Python/OS versions) instead of a
# one-line error.  Kept as a source constant so behavior is deterministic and
# testable without environment plumbing.
GDR_DEBUG = False

# Only meaningful together with ``GDR_DEBUG``: also re-raise the exception
# after printing the debug diagnostic, so a reported bug can be surfaced or
# exercised by tests.
GDR_PROPAGATE_EXCEPTION = False

# Upper bound on intrusive-list / registry walks.  Guards against a corrupt
# or cyclic linked list hanging GDB; diagnostics that exceed this report
# ``<invalid>`` / truncated results instead of looping forever.
GDR_MAX_TRAVERSAL_COUNT = 4096

# Upper bound on the bytes read for one C string.  Prevents an unterminated
# or bogus pointer from reading an unbounded region of target memory.
GDR_MAX_CSTRING_LENGTH = 256

# Fallback terminal width when neither GDB's ``set width`` nor the system
# terminal size is available.  Used by ``terminal_width`` and as the default
# for ``format_table``.
GDR_DEFAULT_TERMINAL_WIDTH = 120

# Columns between adjacent table columns (two spaces).
GDR_TABLE_COLUMN_GAP = 2

# Smallest rendered width for an elastic text column.  A header shorter than
# this still pads to the minimum so the two-dot truncation suffix stays
# visually distinct.
GDR_MIN_ELASTIC_COLUMN_WIDTH = 5

# Suffix appended to a cell that is truncated to fit an elastic column width.
GDR_TABLE_TRUNCATION_SUFFIX = ".."


# ---------------------------------------------------------------------------
# Machine-readable API: the envelope and its evaluate transport
# ---------------------------------------------------------------------------

# Envelope schema version.  Bump only on a breaking change to the envelope or
# the chunk protocol; it is deliberately independent of the GDR release version
# so a client compares it against its own compiled expectation instead of
# parsing release strings.
GDR_API_SCHEMA_VERSION = 1

# Payloads up to this many characters ride one ``$gdr_json`` evaluate result;
# larger ones switch to the chunked protocol.  The limit has to stay below what
# a DAP-facing evaluate returns intact, because GDB's print-element limit and
# MI varobj truncation both apply to the result string.
GDR_API_INLINE_LIMIT = 2048

# Base64 characters per chunk frame.  Well below GDB's default
# ``set print elements`` (200), so the session's print settings never need to be
# touched, and free of quotes and control bytes, so no evaluate-result escaping
# can mangle a frame.
GDR_API_CHUNK_SIZE = 128
