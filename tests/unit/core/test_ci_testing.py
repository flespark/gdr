"""Host-only regression checks for the public integration-test framework."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from ci.testing import cache, cli, containers, matrix, runner


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    location = root / "ci/.cache"
    for module in (cache, cli, containers, matrix, runner):
        monkeypatch.setattr(module, "CACHE", location)
    for module in (cache, containers, runner):
        monkeypatch.setattr(module, "ROOT", root)
    return location


def test_cache_initialize_creates_owned_directories(isolated_cache):
    assert cache.initialize() == isolated_cache
    assert all((isolated_cache / name).is_dir() for name in cache.DIRECTORIES)
    cache.initialize()


def test_cache_external_link_is_idempotent(isolated_cache, tmp_path):
    destination = tmp_path / "external storage"
    cache.initialize(destination)
    assert isolated_cache.is_symlink()
    assert isolated_cache.resolve() == destination
    cache.initialize(destination)
    with pytest.raises(ValueError, match="already exists"):
        cache.initialize(tmp_path / "different")


@pytest.mark.parametrize("relative", [".", "..", "ci/.cache", "ci/.cache/nested"])
def test_cache_rejects_recursive_or_ancestor_destination(isolated_cache, relative):
    with pytest.raises(ValueError, match="recurse"):
        cache.initialize(cache.ROOT / relative)
    assert not isolated_cache.exists()


def test_cache_preserves_existing_contents(isolated_cache, tmp_path):
    isolated_cache.mkdir(parents=True)
    sentinel = isolated_cache / "owned.txt"
    sentinel.write_text("preserve")
    with pytest.raises(ValueError, match="already exists"):
        cache.initialize(tmp_path / "external")
    assert sentinel.read_text() == "preserve"


def test_cache_rejects_broken_link(isolated_cache, tmp_path):
    isolated_cache.parent.mkdir(parents=True)
    isolated_cache.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(ValueError, match="broken link"):
        cache.initialize()


def test_cache_first_run_prompts_only_when_interactive(
    isolated_cache, monkeypatch, tmp_path
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    destination = tmp_path / "interactive"
    prompt = Mock(return_value=str(destination))
    monkeypatch.setattr("builtins.input", prompt)
    cache.initialize(prompt=True)
    assert isolated_cache.resolve() == destination
    cache.initialize(prompt=True)
    prompt.assert_called_once()


def test_lane_defaults_and_metadata_are_explicit():
    assert matrix.select("freertos", "qemu-virt-rv64", "V11.1.0").variant == "rv64"
    assert matrix.select("rtthread", "cortex-a9", "v4.0.5").variant == ""
    assert (
        matrix.select("freertos", "mps2-an385", "10.3.1").tag == "V10.3.1-kernel-only"
    )
    snapshot = matrix.select("freertos", "mps2-an521", "11.1.0", "snapshot")
    assert snapshot.qemu is None
    assert snapshot.artifacts == ("freertos.elf", "snapshot_heap.elf")
    metadata = matrix.select("freertos", "qemu-virt-rv64", "11.1.0", "eh-frame")
    assert metadata.architecture == "riscv:rv64"
    assert "frames.txt" in metadata.artifacts
    with pytest.raises(ValueError, match="unsupported lane"):
        matrix.select("freertos", "qemu-virt-rv64", "11.1.0", "base")


def test_lane_catalog_preserves_compile_only_policy():
    assert len(matrix.SUPPORTED) == len(set(matrix.SUPPORTED))
    for lane in matrix.SUPPORTED:
        assert matrix.select(lane.rtos, lane.target, lane.version, lane.variant) == lane
    assert matrix.matrix_build_only(matrix.select("rtthread", "cortex-a9", "3.1.2"))
    assert not matrix.matrix_build_only(matrix.select("rtthread", "rv64", "4.0.5"))


@pytest.mark.parametrize("target", ["mps2-an385", "mps2-an505", "rv32"])
def test_rtthread_port_fixture_uses_the_shared_lane_contract(target):
    lane = matrix.select("rtthread", target, "v4.0.5")
    assert lane.variant == "" and lane.tag == "v4.0.5"
    if target == "rv32":
        assert lane.architecture == "riscv:rv32" and lane.qemu == "qemu-system-riscv32"
        assert lane.artifacts == ("rtthread.elf", "rtthread.bin", ".debug-macros")
    else:
        assert lane.architecture == "arm" and lane.qemu == "qemu-system-arm"
        assert lane.artifacts == ("rtthread.elf", ".debug-macros")
    for group in ("rtthread-ports", "rtthread", "smoke", "all"):
        assert lane in matrix.GROUPS[group]
    with pytest.raises(ValueError, match="unsupported lane"):
        matrix.select("rtthread", target, "4.1.1")


def test_rtthread_armv7m_patches_preserve_upstream_context_code():
    root = Path(__file__).resolve().parents[3]
    patches = root / "ci/rt-thread/patches/mps2-an385/4.0.5"
    changed = set()
    for patch in sorted(patches.glob("*.patch")):
        output = subprocess.check_output(
            ["git", "apply", "--numstat", str(patch)], text=True
        )
        for line in output.splitlines():
            added, removed, path = line.split("\t", 2)
            assert int(added) > 0 and removed == "0"
            assert path.startswith("bsp/gdr-mps2-an385/")
            changed.add(Path(path).name)
    assert changed == {
        "SConstruct",
        "SConscript",
        "rtconfig.py",
        "rtconfig.h",
        "board.c",
        "startup.S",
        "linker.ld",
        "main.c",
        "irq.c",
    }


def test_microbit_fixture_compatibility_changes_are_explicit_and_do_not_use_systick():
    root = Path(__file__).resolve().parents[3]
    patches = root / "ci/rt-thread/patches/microbit/4.0.5"
    for path in patches.glob("*.patch"):
        subprocess.check_output(["git", "apply", "--numstat", str(path)])
    compat = (patches / "003-port-compat.patch").read_text()
    assert "+    MOVS    R0, #0x03" in compat
    assert (
        "+    stack_frame->exception_stack_frame.pc  = (unsigned long)tentry & ~1UL;"
        in compat
    )
    assert "exception_stack_frame.lr" not in "\n".join(
        line for line in compat.splitlines() if line.startswith("+")
    )
    bsp = (patches / "001-qemu-bsp.patch").read_text()
    assert "0x4000A000u" in bsp and "+void Timer2_Handler(void)" in bsp
    assert "REG32(TICK_TIMER2 + 0x200) = 1" in bsp
    assert "SysTick_Handler" not in bsp
    assert not any(
        address in bsp for address in ("0xE000E010", "0xE000E014", "0xE000E018")
    )


@pytest.mark.parametrize("target", ["mps2-an505", "rv32"])
def test_new_port_bsp_and_application_patches_only_add_fixture_files(target):
    root = Path(__file__).resolve().parents[3]
    patches = root / f"ci/rt-thread/patches/{target}/4.0.5"
    changed = set()
    for name in ("001-qemu-bsp.patch", "002-test-fixture.patch"):
        output = subprocess.check_output(
            ["git", "apply", "--numstat", str(patches / name)], text=True
        )
        for line in output.splitlines():
            added, removed, path = line.split("\t", 2)
            assert int(added) > 0 and removed == "0"
            assert path.startswith(f"bsp/gdr-{target}/")
            changed.add(Path(path).name)
    assert changed == {
        "SConstruct",
        "SConscript",
        "rtconfig.py",
        "rtconfig.h",
        "board.c",
        "startup.S",
        "linker.ld",
        "main.c",
    }
    if target == "mps2-an505":
        compat = (patches / "003-port-compat.patch").read_text()
        subprocess.check_output(
            ["git", "apply", "--numstat", str(patches / "003-port-compat.patch")]
        )
        assert "+.cpu cortex-m33" in compat
        assert "+    stack_frame->lr = 0xFFFFFFFD;" in compat
    else:
        assert len(list(patches.glob("*.patch"))) == 2
        bsp = (patches / "001-qemu-bsp.patch").read_text()
        assert (
            "libcpu/risc-v/common" in bsp
            and "libcpu/risc-v/e310/interrupt_gcc.S" in bsp
        )


def test_cli_list_and_invalid_coordinates_have_no_cache_side_effects(
    isolated_cache, capsys
):
    assert cli.main(["list", "smoke"]) == 0
    assert "freertos/qemu-virt-rv64/11.1.0/eh-frame" in capsys.readouterr().out
    assert cli.main(["run", "freertos", "bad-board", "11.1.0"]) == 1
    assert not isolated_cache.exists()


def test_cli_forwards_only_after_separator(monkeypatch):
    monkeypatch.setattr(runner, "register_tools", Mock())
    run = Mock()
    monkeypatch.setattr(runner, "run", run)
    node = "tests/integration/freertos/test_backtrace.py::test_saved_context"
    assert (
        cli.main(["run", "freertos", "mps2-an385", "11.1.0", "--", node, "-k", "chain"])
        == 0
    )
    assert run.call_args.args[1] == [node, "-k", "chain"]
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "freertos", "mps2-an385", "11.1.0", "-k", "chain"])
    assert error.value.code == 2


def test_cli_matrix_build_only_does_not_change_explicit_run(monkeypatch):
    monkeypatch.setattr(runner, "register_tools", Mock())
    build, run = Mock(), Mock()
    monkeypatch.setattr(runner, "build", build)
    monkeypatch.setattr(runner, "run", run)
    lane = matrix.select("rtthread", "cortex-a9", "3.1.2")
    monkeypatch.setitem(cli.GROUPS, "unit-only", (lane,))
    assert cli.main(["matrix", "unit-only"]) == 0
    build.assert_called_once_with(lane, rebuild=False)
    run.assert_not_called()
    assert cli.main(["run", "rtthread", "cortex-a9", "3.1.2"]) == 0
    run.assert_called_once_with(lane, [], rebuild=False, compatibility=False)


def test_cli_gdb12_and_failure_status(monkeypatch):
    monkeypatch.setattr(runner, "register_tools", Mock())
    run = Mock()
    monkeypatch.setattr(runner, "run", run)
    assert cli.main(["matrix", "gdb12"]) == 0
    assert run.call_args.kwargs["compatibility"] is True
    run.side_effect = subprocess.CalledProcessError(3, ["pytest"])
    assert cli.main(["matrix", "gdb12"]) == 1


def test_environment_ignores_host_configuration(isolated_cache, monkeypatch):
    monkeypatch.setenv("FREERTOS_FIXTURE_CACHE", "/foreign")
    monkeypatch.setenv("GDR_GDB", "foreign-gdb")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/foreign-env")
    env = runner.environment()
    assert "FREERTOS_FIXTURE_CACHE" not in env and "GDR_GDB" not in env
    for key in ("UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "TMPDIR"):
        assert Path(env[key]).is_relative_to(isolated_cache)
    assert env["PATH"].startswith(str(isolated_cache / "tools"))


def test_register_tools_links_without_copying_or_retargeting(
    isolated_cache, monkeypatch, tmp_path
):
    executable = tmp_path / "toolchain/bin/arm-none-eabi-gcc"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: str(executable) if name == executable.name else None,
    )
    runner.register_tools()
    link = isolated_cache / "tools" / cache.host_id() / "arm"
    assert link.is_symlink() and link.resolve() == executable.parent
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: "/different/bin/gcc" if name == executable.name else None,
    )
    runner.register_tools()
    assert link.resolve() == executable.parent


def test_doctor_fails_without_compiler_before_source_fetch(isolated_cache, monkeypatch):
    monkeypatch.setattr(runner, "command", Mock())
    lane = matrix.select("freertos", "qemu-virt-rv64", "11.1.0")
    with pytest.raises(ValueError, match="missing.*gcc"):
        runner.doctor(lane)
    assert not (isolated_cache / "sources").exists()


def test_lane_lock_rejects_concurrent_access_and_releases_after_error():
    lane = matrix.select("rtthread", "cortex-a9", "4.0.5")
    with (
        runner.lane_lock(lane),
        pytest.raises(ValueError, match="already in use"),
        runner.lane_lock(lane),
    ):
        pytest.fail("acquired the same lane twice")
    with pytest.raises(RuntimeError), runner.lane_lock(lane):
        raise RuntimeError("cancelled")
    with runner.lane_lock(lane):
        pass


@pytest.mark.parametrize(
    "manifest", ["invalid", "null", "[]", "{}", '{"fingerprint":"old"}']
)
def test_fresh_rejects_incomplete_or_invalid_manifests(manifest):
    lane = matrix.select("freertos", "mps2-an385", "11.1.0")
    lane.directory.mkdir(parents=True)
    (lane.directory / "manifest.json").write_text(manifest)
    assert not runner.fresh(lane, "current")


@pytest.mark.parametrize(
    "coordinates",
    [
        ("freertos", "mps2-an385", "11.1.0", "base"),
        ("freertos", "mps2-an521", "11.1.0", "snapshot"),
        ("freertos", "qemu-virt-rv64", "11.1.0", "eh-frame"),
        ("rtthread", "rv64", "4.0.5", ""),
        ("rtthread", "mps2-an385", "4.0.5", ""),
        ("rtthread", "mps2-an505", "4.0.5", ""),
        ("rtthread", "rv32", "4.0.5", ""),
    ],
)
def test_build_publishes_complete_artifacts_and_reuses_manifest(
    isolated_cache, monkeypatch, coordinates
):
    lane = matrix.select(*coordinates)
    monkeypatch.setattr(runner, "fingerprint", lambda _lane: "current")
    monkeypatch.setattr(
        runner, "compiler", lambda _lane: (isolated_cache / "compiler", "cross-")
    )
    source = Mock(return_value=isolated_cache / "source")
    monkeypatch.setattr(runner, "sources", source)
    monkeypatch.setattr(runner, "build_tools", Mock())

    def compile_fixture(args, *, env, log):
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"
        output = (
            Path(args[args.index("--cache-dir") + 1])
            if "--cache-dir" in args
            else log.parent
        )
        output.mkdir(parents=True, exist_ok=True)
        for name in lane.artifacts:
            if name != ".debug-macros":
                (output / name).write_bytes(b"fixture")
        log.write_text("compiled\n")

    command = Mock(side_effect=compile_fixture)
    monkeypatch.setattr(runner, "command", command)
    runner.build(lane)
    assert runner.fresh(lane, "current")
    assert Path(
        json.loads((lane.directory / "manifest.json").read_text())["build"]
    ).is_relative_to(isolated_cache)
    runner.build(lane)
    assert command.call_count == 1
    runner.build(lane, rebuild=True)
    assert command.call_count == 2
    assert source.call_count == (0 if lane.variant == "eh-frame" else 2)
    (lane.directory / lane.artifacts[0]).write_bytes(b"")
    assert not runner.fresh(lane, "current")


def test_failed_build_does_not_publish_manifest(isolated_cache, monkeypatch):
    lane = matrix.select("freertos", "qemu-virt-rv64", "11.1.0", "eh-frame")
    monkeypatch.setattr(runner, "fingerprint", lambda _lane: "current")
    monkeypatch.setattr(runner, "compiler", lambda _lane: (isolated_cache, "cross-"))
    monkeypatch.setattr(runner, "command", Mock())
    monkeypatch.setattr(runner, "build_tools", Mock())
    with pytest.raises(ValueError, match="did not produce"):
        runner.build(lane)
    assert not (lane.directory / "manifest.json").exists()


@pytest.mark.parametrize("dirty,head", [(" M tasks.c", "tagged"), ("", "other")])
def test_source_cache_refuses_modified_checkout(
    isolated_cache, monkeypatch, dirty, head
):
    lane = matrix.select("freertos", "mps2-an385", "11.1.0")
    source = isolated_cache / "sources" / lane.rtos / lane.tag
    source.mkdir(parents=True)
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        Mock(side_effect=["tagged\n", head, dirty.encode() + b"\0", b""]),
    )
    with pytest.raises(ValueError, match="source cache modified"):
        runner.sources(lane)


@pytest.mark.parametrize(
    "change",
    [
        "identical",
        "content",
        "spaces",
        "mode",
        "deleted",
        "staged",
        "untracked",
        "symlink",
    ],
)
def test_source_cache_validates_raw_crlf_blobs(tmp_path, change):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(source), *args])

    git("init", "-q")
    git("config", "user.name", "CI test")
    git("config", "user.email", "ci@example.invalid")
    git("config", "core.autocrlf", "false")
    git("config", "core.filemode", "true")
    path = source / "file with spaces.c"
    original = b"int value;\r\n"
    path.write_bytes(original)
    git("add", ".")
    git("-c", "commit.gpgsign=false", "commit", "-qm", "CRLF source")
    attributes = source / ".git/info/attributes"
    attributes.write_text("*.c text\n")
    path.touch()
    assert runner._source_clean(source)
    if change == "content":
        path.write_bytes(b"int changed;\r\n")
    elif change == "spaces":
        path.write_bytes(b"int value; \r\n")
    elif change == "mode":
        path.chmod(0o755)
    elif change == "deleted":
        path.unlink()
    elif change == "staged":
        path.write_bytes(b"int changed;\r\n")
        git("add", path.name)
        path.write_bytes(original)
    elif change == "untracked":
        (source / "extra.c").write_text("extra")
    elif change == "symlink":
        path.unlink()
        path.symlink_to("missing")
    assert runner._source_clean(source) is (change == "identical")


@pytest.mark.parametrize(
    "extra,expected",
    [
        ([], True),
        (["-k", "backtrace"], True),
        (["tests/integration/freertos/test_backtrace.py::test_chain"], False),
    ],
)
def test_pytest_forwarding_and_live_panel_coverage(monkeypatch, extra, expected):
    node = cache.ROOT / "tests/integration/freertos/test_backtrace.py"
    node.parent.mkdir(parents=True)
    node.touch()
    monkeypatch.setattr(runner, "gdb_for", lambda _lane: "cached-gdb")
    calls = []

    def command(args, *, env, log=None):
        calls.append((args, env))
        if log is not None:
            assert log.name == "pytest.log"
        if "pytest" in args:
            report = Path(args[args.index("--junitxml") + 1])
            report.write_text(
                '<testsuites><testsuite><testcase name="executed"/></testsuite></testsuites>'
            )

    monkeypatch.setattr(runner, "command", command)
    lane = matrix.select("freertos", "mps2-an385", "11.1.0")
    runner._pytest(lane, extra)
    args, env = calls[-1]
    assert ("tests/integration/test_panel_tables.py" in args) is expected
    assert ("tests/integration/freertos" in args) is expected
    assert args[-len(extra) :] == extra if extra else True
    assert env["GDR_FIXTURE_VARIANT"] == "base"
    assert "--python" in calls[0][0]


@pytest.mark.parametrize("variant", ["snapshot", "eh-frame"])
def test_metadata_and_snapshot_runs_reject_all_skipped(monkeypatch, variant):
    monkeypatch.setattr(runner, "gdb_for", lambda _lane: "cached-gdb")
    target = "mps2-an521" if variant == "snapshot" else "qemu-virt-rv64"
    lane = matrix.select("freertos", target, "11.1.0", variant)

    def command(args, *, env, log=None):
        assert env["GDR_FIXTURE_VARIANT"] == variant
        if log is not None:
            assert log.name == "pytest.log"
        if "pytest" in args:
            assert "tests/integration/test_panel_tables.py" not in args
            report = Path(args[args.index("--junitxml") + 1])
            report.write_text("<testsuite><testcase><skipped/></testcase></testsuite>")

    monkeypatch.setattr(runner, "command", command)
    with pytest.raises(ValueError, match="no tests executed"):
        runner._pytest(lane, [])
    runner._pytest(lane, ["--collect-only"])


def test_container_environment_removes_external_connection_overrides(
    isolated_cache, monkeypatch
):
    monkeypatch.setenv("CONTAINER_HOST", "remote")
    monkeypatch.setenv("DOCKER_HOST", "remote")
    env = containers.container_environment()
    assert "CONTAINER_HOST" not in env and "DOCKER_HOST" not in env
    assert all(
        Path(env[key]).is_relative_to(isolated_cache)
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")
    )


def test_container_preserves_external_cache_symlink_paths(tmp_path, monkeypatch):
    destination = tmp_path / "external cache"
    cache.initialize(destination)
    command = Mock()
    monkeypatch.setattr(containers, "command", command)
    monkeypatch.setattr(
        containers.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0)),
    )
    containers.container_command("run", "qemu", "linux/arm64", True, ["list", "smoke"])
    args = command.call_args.args[0]
    assert f"{destination}:{destination}" in args
    assert f"{cache.ROOT}:{cache.ROOT}" in args
    assert args[-2:] == ["list", "smoke"]
    assert "linux/arm64" in args


def test_container_gdb12_rejects_wrong_architecture_and_absent_images(monkeypatch):
    with pytest.raises(ValueError, match="amd64 only"):
        containers.build_arguments("gdb12", "linux/arm64")
    monkeypatch.setattr(
        containers.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1)),
    )
    with pytest.raises(ValueError, match="image missing"):
        containers.container_command("run", "qemu", "linux/amd64", True, [])
