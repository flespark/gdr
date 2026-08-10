"""Pytest fixtures for profile-driven QEMU closed-loop verification.

The selected RTOS owns its target profile while :mod:`tests.qemu_harness`
owns the QEMU/GDB process lifecycle. RT-Thread's old environment variables
remain accepted for one release cycle through ``rtthread_qemu_profiles``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.qemu_harness import GdbSession, QemuProfile, QemuSession, check_tools
from tests.rtthread_qemu_profiles import get_rtthread_qemu_profile

GDR_ROOT = Path(__file__).resolve().parent.parent
GDB_BIN = os.environ.get("GDR_GDB", "gdb")
BOOT_WAIT = float(os.environ.get("GDR_BOOT_WAIT", "10"))


def get_qemu_profile() -> QemuProfile:
    """Resolve the selected RTOS profile without coupling the harness to it."""
    rtos = os.environ.get("GDR_RTOS", "rtthread")
    if rtos == "rtthread":
        return get_rtthread_qemu_profile(GDR_ROOT)
    if rtos == "freertos":
        from tests.freertos_profiles import get_freertos_qemu_profile

        return get_freertos_qemu_profile(GDR_ROOT)
    raise RuntimeError(f"unknown GDR_RTOS: {rtos}")


PROFILE = get_qemu_profile()


@pytest.fixture(scope="session")
def qemu_profile() -> QemuProfile:
    """Expose the selected profile to integration tests."""
    return PROFILE


@pytest.fixture(scope="session")
def qemu(qemu_profile: QemuProfile):
    """Session-scoped QEMU instance with a dynamically allocated GDB port."""
    check_tools(qemu_profile, GDB_BIN)
    session = QemuSession(qemu_profile)
    session.start(BOOT_WAIT)
    yield session
    session.stop()


@pytest.fixture(scope="session")
def gdb(qemu: QemuSession, qemu_profile: QemuProfile):
    """Persistent GDB session that sources ``gdr.py`` exactly once."""
    session = GdbSession(GDB_BIN, qemu_profile, qemu.gdb_port, GDR_ROOT)
    session.start()
    yield session
    session.stop()


@pytest.fixture
def gdb_session(gdb: GdbSession) -> GdbSession:
    """Return the session-scoped GDB command runner for one test."""
    return gdb
