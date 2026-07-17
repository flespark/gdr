"""Test aggregate commands produce correct tabular output.

Asserts that ``rtthread threads``, ``rtthread semaphores``, ``rtthread
timers``, ``rtthread objects``, and ``rtthread system`` list the
expected test-fixture objects.
"""

from __future__ import annotations

import re


class TestThreadsCommand:
    """``rtthread threads`` output."""

    def test_lists_test_threads(self, gdb_session):
        """Output contains worker1, worker2, worker3.

        Note: ``main`` thread exits after ``main()`` returns and is
        removed from the thread list — it is NOT expected here.
        """
        out = gdb_session.run("rtthread threads")
        for name in ["worker1", "worker2", "worker3"]:
            assert name in out, f"thread {name!r} not found in output:\n{out}"

    def test_has_table_headers(self, gdb_session):
        """Output has Name, State, Prio, StkUsed, and MaxStkUsed headers."""
        out = gdb_session.run("rtthread threads")
        for header in ["Name", "State", "Prio", "StkUsed", "MaxStkUsed"]:
            assert header in out, f"header {header!r} missing in output:\n{out}"

    def test_stack_used_matches_worker_stack_fields(self, gdb_session):
        """StkUsed equals ``stack_size - (sp - stack_addr)`` for worker1."""
        out = gdb_session.run_python(
            """
import gdb

thread = gdb.parse_and_eval('$gdr_thread("worker1")')
stack_used = int(thread["stack_size"]) - (int(thread["sp"]) - int(thread["stack_addr"]))
stack = bytes(gdb.selected_inferior().read_memory(int(thread["stack_addr"]), int(thread["stack_size"])))
print(f"stack_used={stack_used}")
print(f"max_stack_used={len(stack.lstrip(b'#'))}")
"""
        )
        stack_used = re.search(r"stack_used=(\d+)", out)
        max_stack_used = re.search(r"max_stack_used=(\d+)", out)
        assert stack_used is not None, out
        assert max_stack_used is not None, out

        out = gdb_session.run("rtthread threads")
        worker_row = next(
            line for line in out.splitlines() if line.lstrip().startswith("worker1")
        )
        assert worker_row.split()[-3] == stack_used.group(1), worker_row
        assert worker_row.split()[-2] == max_stack_used.group(1), worker_row

    def test_thread_states_valid(self, gdb_session):
        """All thread states in the table are known values."""
        out = gdb_session.run("rtthread threads")
        valid_states = ["suspend", "ready", "running", "init", "close"]
        lines = out.strip().split("\n")

        # Only parse lines within the table (after the "---" separator)
        in_table = False
        data_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("---"):
                in_table = True
                continue
            if in_table:
                # Stop at empty line or GDB messages
                if not stripped or stripped.startswith("["):
                    break
                data_lines.append(line)

        assert len(data_lines) > 0, "no thread data rows found"
        for line in data_lines:
            parts = line.split()
            assert len(parts) >= 2, f"unexpected row format: {line!r}"
            state = next(
                (
                    part.rstrip("*")
                    for part in parts
                    if part.rstrip("*") in valid_states
                ),
                None,
            )
            assert state is not None, f"unknown state in line: {line!r}"


class TestSemaphoresCommand:
    """``rtthread semaphores`` output."""

    def test_lists_test_sem(self, gdb_session):
        """Output contains test_sem."""
        out = gdb_session.run("rtthread semaphores")
        assert "test_sem" in out, f"test_sem not found in output:\n{out}"

    def test_has_value_column(self, gdb_session):
        """Output has Name, Value headers."""
        out = gdb_session.run("rtthread semaphores")
        for header in ["Name", "Value"]:
            assert header in out


class TestTimersCommand:
    """``rtthread timers`` output."""

    def test_lists_test_timer(self, gdb_session):
        """Output contains test_timer (may be truncated to test_tim)."""
        out = gdb_session.run("rtthread timers")
        assert "test_tim" in out or "test_timer" in out, (
            f"test_timer not found in output:\n{out}"
        )

    def test_test_timer_is_periodic_soft(self, gdb_session):
        """test_timer shows periodic + soft mode."""
        out = gdb_session.run("rtthread timers")
        lines = out.split("\n")
        for line in lines:
            if "test_tim" in line or "test_timer" in line:
                assert "periodic" in line.lower(), f"expected periodic in: {line!r}"
                assert "soft" in line.lower(), f"expected soft in: {line!r}"
                break
        else:
            raise AssertionError(f"test_timer row not found in output:\n{out}")


class TestObjectsCommand:
    """``rtthread objects`` output."""

    def test_summary_lists_types(self, gdb_session):
        """``rtthread objects`` shows all enabled type counts."""
        out = gdb_session.run("rtthread objects")
        for type_name in ["thread", "semaphore", "mutex", "timer"]:
            assert type_name in out, (
                f"type {type_name!r} missing in objects output:\n{out}"
            )

    def test_filtered_by_type(self, gdb_session):
        """``rtthread objects thread`` shows thread count."""
        out = gdb_session.run("rtthread objects thread")
        assert "thread" in out.lower()

    def test_unknown_type_warns(self, gdb_session):
        """``rtthread objects invalid`` prints a warning."""
        out = gdb_session.run("rtthread objects invalid")
        assert "warning" in out.lower() or "unknown" in out.lower()


class TestSystemCommand:
    """``rtthread system`` output."""

    def test_shows_kernel_tick(self, gdb_session):
        """Output contains 'Kernel tick'."""
        out = gdb_session.run("rtthread system")
        assert "Kernel tick" in out or "tick" in out.lower()

    def test_shows_object_counts(self, gdb_session):
        """Output contains thread and semaphore counts."""
        out = gdb_session.run("rtthread system")
        assert "thread" in out.lower()
        assert "semaphore" in out.lower() or "sem" in out.lower()
