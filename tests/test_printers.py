"""Test pretty-printers fold kernel structs into one-line summaries.

Uses convenience functions (``$gdr_thread``, ``$gdr_object``) to
obtain struct values, then verifies ``p`` output contains the folded
format ``TypeName(field=value, ...)`` with the expected summary fields.
"""

from __future__ import annotations


class TestPrinters:
    """Pretty-printer registration and folding output."""

    def test_printer_registered(self, gdb_session):
        """Printing a known struct produces a folded summary, not a raw dump."""
        out = gdb_session.run('p $gdr_thread("worker1")')
        # If printers work, we see "Thread(...)" instead of a raw struct dump
        assert "Thread(" in out, f"pretty-printer not active, got:\n{out}"

    def test_thread_prints_as_thread(self, gdb_session):
        """``p $gdr_thread("worker1")`` output contains ``Thread(``."""
        out = gdb_session.run('p $gdr_thread("worker1")')
        assert "Thread(" in out, f"expected Thread( fold, got:\n{out}"
        # Summary should include name and state
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_semaphore_folds(self, gdb_session):
        """``p $gdr_object(0x02, "test_sem")`` prints ``Semaphore(...)``."""
        out = gdb_session.run('p $gdr_object(0x02, "test_sem")')
        assert "Semaphore(" in out, f"expected Semaphore( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_mutex_folds(self, gdb_session):
        """``p $gdr_object(0x03, "test_mut")`` prints ``Mutex(...)``.

        Note: "test_mutex" is truncated to "test_mut" by RT_NAME_MAX=8.
        """
        out = gdb_session.run('p $gdr_object(0x03, "test_mut")')
        assert "Mutex(" in out, f"expected Mutex( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_timer_folds(self, gdb_session):
        """``p $gdr_object(0x0a, "test_tim")`` prints ``Timer(...)``.

        Note: "test_timer" is truncated to "test_tim" by RT_NAME_MAX=8.
        """
        out = gdb_session.run('p $gdr_object(0x0a, "test_tim")')
        assert "Timer(" in out, f"expected Timer( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"
