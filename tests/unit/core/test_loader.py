"""Unit tests for the closed-loop integration environment loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.loader import load_integration_spec


def test_rtthread_defaults():
    spec = load_integration_spec({})
    assert spec.rtos == "rtthread"
    assert spec.version == "4.0.5"
    assert spec.target == "cortex-a9"
    assert spec.variant == ""
    assert (
        spec.elf_path() == spec.fixture_cache / "cortex-a9" / "4.0.5" / "rtthread.elf"
    )
    assert spec.firmware_path() == spec.elf_path()


def test_rtthread_rv64_firmware_is_the_bin():
    spec = load_integration_spec(
        {"GDR_RTOS": "rtthread", "GDR_QEMU_TARGET": "rv64", "GDR_VERSION": "4.1.1"}
    )
    assert spec.firmware_path() == spec.fixture_dir() / "rtthread.bin"
    assert spec.elf_path().name == "rtthread.elf"


def test_blank_overrides_mean_defaults():
    spec = load_integration_spec({"GDR_RTOS": "", "GDR_VERSION": "", "GDR_GDB": ""})
    assert spec.rtos == "rtthread"
    assert spec.version == "4.0.5"
    assert spec.gdb == "gdb"


def test_freertos_live_cache_layout():
    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_QEMU_TARGET": "mps2-an385",
            "GDR_VERSION": "10.4.6",
            "GDR_FIXTURE_VARIANT": "base",
            "FREERTOS_FIXTURE_CACHE": "/tmp/cache",
        }
    )
    assert spec.fixture_dir() == Path("/tmp/cache/mps2-an385/10.4.6/base")
    assert spec.elf_path() == spec.fixture_dir() / "freertos.elf"
    assert spec.firmware_path() == spec.elf_path()


def test_freertos_rv64_firmware_is_the_bin():
    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_QEMU_TARGET": "qemu-virt-rv64",
            "GDR_VERSION": "11.1.0",
            "GDR_FIXTURE_VARIANT": "rv64",
        }
    )
    assert spec.firmware_path() == spec.fixture_dir() / "freertos.bin"


def test_snapshot_uses_snapshot_cache_not_a_board_path():
    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_FIXTURE_VARIANT": "snapshot",
            "GDR_QEMU_TARGET": "mps2-an385",
            "GDR_VERSION": "10.4.6",
            "FREERTOS_FIXTURE_CACHE": "/tmp/cache",
        }
    )
    assert spec.variant == "snapshot"
    assert spec.version == "10.4.6"
    assert spec.fixture_dir() == Path("/tmp/cache/snapshot")


def test_snapshot_default_version_is_11_1_0():
    spec = load_integration_spec(
        {"GDR_RTOS": "freertos", "GDR_FIXTURE_VARIANT": "snapshot"}
    )
    assert spec.version == "11.1.0"


def test_elf_override_repoints_gdb_and_the_qemu_boot_image():
    # ARM lane: QEMU boots the same ELF via -kernel.
    arm = load_integration_spec(
        {"GDR_RTOS": "freertos", "GDR_ELF_PATH": "/tmp/custom.elf"}
    )
    assert arm.elf_path() == Path("/tmp/custom.elf")
    assert arm.firmware_path() == Path("/tmp/custom.elf")
    # RV64 lane: QEMU boots the raw .bin sibling via -bios.
    rv64 = load_integration_spec(
        {
            "GDR_RTOS": "rtthread",
            "GDR_QEMU_TARGET": "rv64",
            "GDR_ELF_PATH": "/tmp/build/rtthread.elf",
        }
    )
    assert rv64.elf_path() == Path("/tmp/build/rtthread.elf")
    assert rv64.firmware_path() == Path("/tmp/build/rtthread.bin")


def test_unknown_rtos_is_an_error():
    with pytest.raises(RuntimeError, match="unknown GDR_RTOS"):
        load_integration_spec({"GDR_RTOS": "zephyr"})


def test_rtthread_profile_uses_loader_paths():
    from tests.support.rtthread_qemu_profiles import get_rtthread_qemu_profile

    spec = load_integration_spec(
        {
            "GDR_RTOS": "rtthread",
            "GDR_QEMU_TARGET": "rv64",
            "GDR_VERSION": "4.1.1",
            "RT_THREAD_FIXTURE_CACHE": "/tmp/rtt",
        }
    )
    profile = get_rtthread_qemu_profile(Path("/unused"), spec)
    assert profile.machine == "virt"
    assert profile.firmware_option == "-bios"
    assert profile.elf_path == Path("/tmp/rtt/rv64/4.1.1/rtthread.elf")
    assert profile.firmware_path == Path("/tmp/rtt/rv64/4.1.1/rtthread.bin")


def test_freertos_profile_derives_machine_from_target():
    from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile

    spec = load_integration_spec(
        {
            "GDR_RTOS": "freertos",
            "GDR_QEMU_TARGET": "qemu-virt-rv64",
            "GDR_VERSION": "11.1.0",
            "GDR_FIXTURE_VARIANT": "rv64",
            "FREERTOS_FIXTURE_CACHE": "/tmp/frt",
        }
    )
    profile = get_freertos_qemu_profile(Path("/unused"), spec)
    assert profile.machine == "virt"
    assert profile.firmware_option == "-bios"
    assert profile.elf_path == Path("/tmp/frt/qemu-virt-rv64/11.1.0/rv64/freertos.elf")
    assert profile.firmware_path == Path(
        "/tmp/frt/qemu-virt-rv64/11.1.0/rv64/freertos.bin"
    )


def test_snapshot_has_no_qemu_profile():
    from tests.support.freertos_qemu_profiles import get_freertos_qemu_profile

    spec = load_integration_spec(
        {"GDR_RTOS": "freertos", "GDR_FIXTURE_VARIANT": "snapshot"}
    )
    with pytest.raises(RuntimeError, match="no QEMU profile"):
        get_freertos_qemu_profile(Path("/unused"), spec)
