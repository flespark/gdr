"""Test convenience functions return valid gdb.Value objects.

Asserts that ``$gdr_thread(name)`` and ``$gdr_object(type, name)``
return usable values.  Uses the persistent GDB session so convenience
functions are registered once and available across all tests.
"""

from __future__ import annotations

import os

_DEFAULT_POINTER_BYTES = "8" if os.environ.get("GDR_QEMU_TARGET") == "rv64" else "4"
EXPECTED_POINTER_BYTES = int(
    os.environ.get("GDR_EXPECT_POINTER_BYTES", _DEFAULT_POINTER_BYTES)
)


class TestConvenienceFunctions:
    """GDB convenience function ($gdr_*) correctness."""

    def test_gdr_thread_worker1(self, gdb_session):
        """``$gdr_thread("worker1")`` returns a non-null value.

        The pretty-printer should fold it into ``Thread(name=...)``.
        """
        out = gdb_session.run('p $gdr_thread("worker1")')
        # Non-null: GDB prints the struct (folded by pretty-printer)
        # or at least a non-zero value.
        assert "= 0" not in out or "Thread(" in out, (
            f"expected non-null thread, got:\n{out}"
        )

    def test_gdr_thread_field_access(self, gdb_session):
        """``$gdr_thread("worker1").current_priority`` is 20."""
        out = gdb_session.run('p $gdr_thread("worker1").current_priority')
        # GDB prints "$N = 20" for the priority field
        assert "20" in out, f"expected priority 20, got:\n{out}"

    def test_gdr_thread_name_field(self, gdb_session):
        """``$gdr_thread("worker1").name`` contains "worker1"."""
        out = gdb_session.run('p $gdr_thread("worker1").name')
        assert "worker1" in out, f"expected name 'worker1', got:\n{out}"

    def test_target_pointer_width(self, gdb_session):
        """The connected firmware has the expected native pointer width."""
        out = gdb_session.run("p sizeof(void *)")
        assert f"= {EXPECTED_POINTER_BYTES}" in out, (
            f"expected {EXPECTED_POINTER_BYTES}-byte pointers, got:\n{out}"
        )

    def test_gdr_object_semaphore(self, gdb_session):
        """``$gdr_object(0x02, "test_sem")`` returns a non-null value."""
        out = gdb_session.run('p $gdr_object(0x02, "test_sem")')
        assert "= 0" not in out or "Semaphore(" in out, (
            f"expected non-null semaphore, got:\n{out}"
        )

    def test_gdr_thread_not_found(self, gdb_session):
        """``$gdr_thread("nonexistent")`` returns 0 (not found)."""
        out = gdb_session.run('p $gdr_thread("nonexistent")')
        # GDB prints "$N = 0" for null return
        assert "= 0" in out, f"expected 0 for nonexistent thread, got:\n{out}"
