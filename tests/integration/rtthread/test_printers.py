"""Test pretty-printers fold kernel structs into one-line summaries.

Uses the stable convenience functions (``$gdr_task``, ``$gdr_object``) to
obtain struct values, then verifies ``p`` output contains the folded
format ``TypeName(field=value, ...)`` with the expected summary fields.

COUPLED: fixture object names, timer flags and owner relationships come from
the matching patch in ``ci/rt-thread/patches/<target>/<version>/``. Changes to
those patches require reviewing these assertions together.
"""

from __future__ import annotations

import os

import pytest

from tests.support.rtthread_profiles import get_rtthread_test_profile

# Symbolic thread state names (mirror rtthread.layout.ThreadState).
# The printer's enum_map must render the raw stat int as one of these.
_THREAD_STATE_SYMBOLS = {"INIT", "READY", "SUSPEND", "RUNNING", "CLOSE"}
_IS_RV64 = os.environ.get("GDR_QEMU_TARGET") == "rv64"
_RTTHREAD_VERSION = os.environ.get("GDR_RTTHREAD_VERSION", "4.0.5")
_PROFILE = get_rtthread_test_profile(
    _RTTHREAD_VERSION, "rv64" if _IS_RV64 else "cortex-a9"
)

pytestmark = pytest.mark.skipif(
    os.environ.get("GDR_RTOS", "rtthread") != "rtthread",
    reason="requires an RT-Thread QEMU profile",
)


class TestPrinters:
    """Pretty-printer registration and folding output."""

    def test_thread_folds_with_summary_fields(self, gdb_session):
        """A thread folds with its name and a symbolic state.

        Regression guard for the enum_map feature in
        ``gdr.printers._format_field``: before the map the fold showed
        ``stat=2``; afterwards it shows ``stat=SUSPEND``.
        """
        out = gdb_session.run('p $gdr_task("worker1")')
        assert "Thread(" in out, f"expected Thread( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"
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
        """A semantic semaphore lookup prints ``Semaphore(...)``."""
        out = gdb_session.run(
            f'p $gdr_object("semaphore", "{_PROFILE.semaphore_name}")'
        )
        assert "Semaphore(" in out, f"expected Semaphore( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"

    def test_mutex_folds(self, gdb_session):
        """A semantic mutex lookup prints ``Mutex(...)``."""
        out = gdb_session.run(f'p $gdr_object("mutex", "{_PROFILE.mutex_name}")')
        assert "Mutex(" in out, f"expected Mutex( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"
        assert 'owner=\\"worker1\\"' in out, f"expected dereferenced owner, got:\n{out}"

    def test_function_pointer_symbolic(self, gdb_session):
        """A function pointer is rendered as its symbol and offset."""
        out = gdb_session.run_python(
            """
import gdb
from gdr.printers import _format_field
from gdr.layout import StructField

entry = gdb.parse_and_eval('$gdr_task("worker1").entry')
print(_format_field(entry, StructField("entry", ("entry",), kind="ptr")))
"""
        )
        assert "<" in out and ">" in out, f"expected symbolized pointer, got:\n{out}"

    def test_timer_folds_with_symbolic_flags(self, gdb_session):
        """Timer output folds fields and renders ACTIVE/PERIODIC/SOFT flags.

        The test fixture installs ``test_timer`` as a periodic soft timer,
        so the fold must show ``ACTIVE`` and ``PERIODIC`` and ``SOFT`` rather
        than a bare ``0x7``.
        """
        out = gdb_session.run(f'p $gdr_object("timer", "{_PROFILE.timer_name}")')
        assert "Timer(" in out, f"expected Timer( fold, got:\n{out}"
        assert "name=" in out, f"expected name= field, got:\n{out}"
        assert "flag=" in out, f"expected flag= field, got:\n{out}"
        # test_timer is periodic + soft + activated per the fixture.
        for bit in ("ACTIVE", "PERIODIC", "SOFT"):
            assert bit in out, f"expected flag bit {bit} in fold, got:\n{out}"
