"""Test pretty-printers fold kernel structs into one-line summaries.

Uses convenience functions (``$gdr_thread``, ``$gdr_object``) to
obtain struct values, then verifies ``p`` output contains the folded
format ``TypeName(field=value, ...)`` with the expected summary fields.
"""

from __future__ import annotations

import os

# Symbolic thread state names (mirror gdr.abstractions.ThreadState).
# The printer's enum_map must render the raw stat int as one of these.
_THREAD_STATE_SYMBOLS = {"INIT", "READY", "SUSPEND", "RUNNING", "CLOSE"}
_IS_RV64 = os.environ.get("GDR_QEMU_TARGET") == "rv64"
MUTEX_NAME = os.environ.get(
    "GDR_TEST_MUTEX_NAME", "test_mutex" if _IS_RV64 else "test_mut"
)
TIMER_NAME = os.environ.get(
    "GDR_TEST_TIMER_NAME", "test_timer" if _IS_RV64 else "test_tim"
)


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

    def test_thread_stat_symbolic(self, gdb_session):
        """``stat`` field renders as a symbolic name, not a raw int.

        Regression guard for the enum_map feature in
        ``gdr.printers._format_field``: before the map the fold showed
        ``stat=2``; afterwards it shows ``stat=SUSPEND``.
        """
        out = gdb_session.run('p $gdr_thread("worker1")')
        assert "stat=" in out, f"expected stat= field, got:\n{out}"
        # Extract the value after ``stat=``
        after = out.split("stat=", 1)[1]
        # Stop at the next ``)`` or ``,`` that delimits the fold field.
        end = min(
            (i for i in (after.find(")"), after.find(",")) if i != -1),
            default=len(after),
        )
        token = after[:end]
        assert token in _THREAD_STATE_SYMBOLS, (
            f"stat value {token!r} not symbolic; expected one of "
            f"{_THREAD_STATE_SYMBOLS}; got:\n{out}"
        )

    def test_semaphore_folds(self, gdb_session):
        """``p $gdr_object(0x02, "test_sem")`` prints ``Semaphore(...)``."""
        out = gdb_session.run('p $gdr_object(0x02, "test_sem")')
        assert "Semaphore(" in out, f"expected Semaphore( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_mutex_folds(self, gdb_session):
        """``p $gdr_object(0x03, mutex_name)`` prints ``Mutex(...)``."""
        out = gdb_session.run(f'p $gdr_object(0x03, "{MUTEX_NAME}")')
        assert "Mutex(" in out, f"expected Mutex( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_timer_folds(self, gdb_session):
        """``p $gdr_object(0x0a, timer_name)`` prints ``Timer(...)``."""
        out = gdb_session.run(f'p $gdr_object(0x0a, "{TIMER_NAME}")')
        assert "Timer(" in out, f"expected Timer( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_timer_flag_symbolic(self, gdb_session):
        """Timer ``flag`` field renders flag-bit names (ACTIVE/PERIODIC/SOFT).

        The test fixture installs ``test_timer`` as a periodic soft timer,
        so the fold must show ``ACTIVE`` and ``PERIODIC`` and ``SOFT`` rather
        than a bare ``0x7``.
        """
        out = gdb_session.run(f'p $gdr_object(0x0a, "{TIMER_NAME}")')
        assert "flag=" in out, f"expected flag= field, got:\n{out}"
        # test_timer is periodic + soft + activated per the fixture.
        for bit in ("ACTIVE", "PERIODIC", "SOFT"):
            assert bit in out, f"expected flag bit {bit} in fold, got:\n{out}"
