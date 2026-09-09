"""Pytest fixtures for profile-driven QEMU closed-loop verification.

:mod:`tests.support.loader` owns environment parsing.
:mod:`tests.support.qemu_harness` owns the QEMU/GDB process lifecycle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.loader import load_integration_spec
from tests.support.qemu_harness import (
    GdbSession,
    QemuProfile,
    QemuSession,
    check_tools,
)
from tests.support.rtthread_qemu_profiles import get_rtthread_qemu_profile

GDR_ROOT = Path(__file__).resolve().parents[2]
SPEC = load_integration_spec()


def get_qemu_profile() -> QemuProfile:
    """Resolve the selected RTOS profile without coupling the harness to it."""
    if SPEC.rtos == "rtthread":
        return get_rtthread_qemu_profile(GDR_ROOT, SPEC)
    if SPEC.rtos == "freertos" and SPEC.variant != "snapshot":
        from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile

        return get_freertos_qemu_profile(GDR_ROOT, SPEC)
    raise RuntimeError(
        f"no QEMU profile for GDR_RTOS={SPEC.rtos!r} variant={SPEC.variant!r}"
    )


@pytest.fixture(scope="session")
def qemu_profile() -> QemuProfile:
    """Expose the selected profile to integration tests."""
    if SPEC.variant == "snapshot":
        pytest.skip("snapshot lane has no QEMU profile")
    return get_qemu_profile()


@pytest.fixture(scope="session")
def qemu(qemu_profile: QemuProfile):
    """Session-scoped QEMU instance with a dynamically allocated GDB port."""
    check_tools(qemu_profile, SPEC.gdb)
    session = QemuSession(qemu_profile)
    session.start(SPEC.boot_wait)
    yield session
    session.stop()


@pytest.fixture(scope="session")
def gdb(qemu: QemuSession, qemu_profile: QemuProfile):
    """Persistent GDB session that sources ``gdr.py`` exactly once."""
    session = GdbSession(SPEC.gdb, qemu_profile, qemu.gdb_port, GDR_ROOT)
    session.start()
    yield session
    session.stop()


@pytest.fixture
def gdb_session(gdb: GdbSession) -> GdbSession:
    """Return the session-scoped GDB command runner for one test."""
    return gdb
