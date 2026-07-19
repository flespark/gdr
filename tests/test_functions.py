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
_IS_RV64 = os.environ.get("GDR_QEMU_TARGET") == "rv64"


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

    def test_container_recovery_preserves_type_and_address(self, gdb_session):
        """List traversal returns the fixture structs after cast + dereference.

        ``$gdr_thread`` and ``$gdr_object`` both recover an owning struct from
        an embedded ``rt_list_t`` node through ``container_of``. Comparing the
        resulting values with known fixture symbols catches pointer truncation,
        bad member offsets, and casts to the wrong struct type on RV64.

        """
        out = gdb_session.run_python(
            """
import gdb

thread = gdb.parse_and_eval('$gdr_thread("worker1")')
semaphore = gdb.parse_and_eval('$gdr_object(0x02, "test_sem")')
print(f"thread_type={thread.type}")
print(f"thread_tag={thread.type.strip_typedefs().tag}")
print(f"thread_is_struct={thread.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT}")
print(f"thread_address_matches={int(thread.address) == int(gdb.parse_and_eval('worker1_thread').address)}")
print(f"semaphore_type={semaphore.type}")
print(f"semaphore_tag={semaphore.type.strip_typedefs().tag}")
print(f"semaphore_is_struct={semaphore.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT}")
print(f"semaphore_address_matches={int(semaphore.address) == int(gdb.parse_and_eval('test_sem').address)}")
"""
        )
        assert "thread_tag=rt_thread" in out, out
        assert "thread_is_struct=True" in out, out
        assert "thread_address_matches=True" in out, out
        assert "semaphore_tag=rt_semaphore" in out, out
        assert "semaphore_is_struct=True" in out, out
        assert "semaphore_address_matches=True" in out, out

    def test_current_thread_matches_selected_cpu(self, gdb_session):
        """``get_current_thread`` follows RT-Thread's SMP per-CPU handle."""
        expected_expr = (
            "rt_current_thread"
            if _IS_RV64
            else "rt_cpu_index(rt_hw_cpu_id())->current_thread"
        )
        out = gdb_session.run_python(
            f"""
import gdb
from rtthread.navigation import get_current_thread

expected = gdb.parse_and_eval({expected_expr!r})
current = get_current_thread()
print(f"expected_non_null={{int(expected) != 0}}")
print(f"current_found={{current is not None}}")
print(f"current_matches_selected_cpu={{current is not None and int(current.address) == int(expected)}}")
"""
        )

        assert "expected_non_null=True" in out, out
        assert "current_found=True" in out, out
        assert "current_matches_selected_cpu=True" in out, out

    def test_target_pointer_width(self, gdb_session):
        """The connected firmware has the expected native pointer width."""
        out = gdb_session.run("p sizeof(void *)")
        assert f"= {EXPECTED_POINTER_BYTES}" in out, (
            f"expected {EXPECTED_POINTER_BYTES}-byte pointers, got:\n{out}"
        )

    def test_arch_info_matches_the_connected_target(self, gdb_session):
        """ArchInfo reports the target pointer width and resolved byte order."""
        out = gdb_session.run_python(
            """
from gdr.gdb_bridge import get_arch_info

arch = get_arch_info()
print(f"arch_found={arch is not None}")
if arch is not None:
    print(f"ptrsize={arch.ptrsize}")
    print(f"endian={arch.endian}")
"""
        )

        assert "arch_found=True" in out, out
        assert f"ptrsize={EXPECTED_POINTER_BYTES}" in out, out
        assert "endian=little" in out, out

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
