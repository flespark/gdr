"""Prepare tools, build disposable fixtures, and invoke selected pytest nodes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .cache import CACHE, ROOT, host_id, initialize
from .matrix import Lane

REPOSITORIES = {
    "freertos": "https://github.com/FreeRTOS/FreeRTOS-Kernel.git",
    "rtthread": "https://github.com/RT-Thread/rt-thread.git",
}

_PROBED: set[tuple[str, ...]] = set()


def _probe(args: list[str], *, env: dict[str, str]) -> None:
    """Run a tool version probe once per process."""
    key = tuple(args)
    if key in _PROBED:
        return
    _PROBED.add(key)
    command(args, env=env)


def environment() -> dict[str, str]:
    """Isolate generated state and ignore retired host-specific overrides."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(
            (
                "GDR_",
                "FREERTOS_",
                "RT_THREAD_",
                "RTOS_TOOLCHAIN_",
                "XPACK_",
                "PYTHON2_",
                "SCONS_",
                "UV_",
            )
        )
    }
    tools = CACHE / "tools" / host_id()
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(key, None)
    env.update(
        {
            "PATH": os.pathsep.join(
                str(tools / name) for name in ("bin", "arm", "riscv")
            )
            + os.pathsep
            + os.environ.get("PATH", os.defpath),
            "UV_PROJECT_ENVIRONMENT": str(CACHE / "env" / host_id() / environment_id()),
            "UV_CACHE_DIR": str(CACHE / "downloads" / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(CACHE / "tools" / host_id() / "python"),
            "TMPDIR": str(CACHE / "tmp"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "RUFF_CACHE_DIR": str(CACHE / "tmp" / "ruff"),
        }
    )
    return env


def environment_id() -> str:
    """Separate image OS/Python ABIs even when CPU architectures match."""
    import platform
    import sys

    distro = platform.freedesktop_os_release() if sys.platform == "linux" else {}
    return f"{distro.get('ID', sys.platform)}-{distro.get('VERSION_ID', 'host')}-py{sys.version_info.major}.{sys.version_info.minor}"


def command(
    args: list[str], *, env: dict[str, str] | None = None, log: Path | None = None
) -> None:
    """Run an argv without a shell; tee output when a log is requested."""
    print(f"[gdr-ci] {shlex.join(map(str, args))}", flush=True)
    if log is None:
        subprocess.run(args, cwd=ROOT, env=env, check=True)
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with (
        log.open("w") as output,
        subprocess.Popen(
            args,
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process,
    ):
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            output.write(line)
        status = process.wait()
    if status:
        raise subprocess.CalledProcessError(status, args)


def register_tools() -> None:
    """Link installed host tools once; never copy a compiler distribution."""
    initialize(prompt=True)
    with cache_lock(f"tools/{host_id()}", wait=True):
        _register_tools()


def _register_tools() -> None:
    """Register discovered tools without replacing existing cache entries."""
    tools = CACHE / "tools" / host_id()
    (tools / "bin").mkdir(parents=True, exist_ok=True)
    candidates = {"arm": "arm-none-eabi-gcc", "riscv": "riscv-none-elf-gcc"}
    for family, binary in candidates.items():
        link = tools / family
        if link.exists() or link.is_symlink():
            continue
        executable = shutil.which(binary)
        if executable:
            link.symlink_to(Path(executable).resolve().parent, target_is_directory=True)
    for name in (
        "uv",
        "qemu-system-arm",
        "qemu-system-riscv32",
        "qemu-system-riscv64",
        "gdb-multiarch",
        "arm-none-eabi-gdb-py3",
        "riscv-none-elf-gdb-py3",
        "gdb",
        "scons",
    ):
        link = tools / "bin" / name
        if (
            not link.exists()
            and not link.is_symlink()
            and (executable := shutil.which(name))
        ):
            link.symlink_to(Path(executable).resolve())
    legacy = tools / "python2"
    if (
        not legacy.exists()
        and not legacy.is_symlink()
        and Path("/opt/python2/bin/python").is_file()
    ):
        legacy.symlink_to("/opt/python2", target_is_directory=True)
    print(f"[gdr-ci] tools: {tools}; missing tools are reported by doctor")


def compiler(lane: Lane) -> tuple[Path, str]:
    family = "arm" if lane.architecture == "arm" else "riscv"
    directory = CACHE / "tools" / host_id() / family
    prefix = "arm-none-eabi-" if family == "arm" else "riscv-none-elf-"
    if not (directory / f"{prefix}gcc").is_file():
        raise ValueError(
            f"missing {directory}/{prefix}gcc; install the toolchain and run setup"
        )
    return directory, prefix


def gdb_for(lane: Lane) -> str:
    tools = CACHE / "tools" / host_id() / "bin"
    specific = (
        "arm-none-eabi-gdb-py3"
        if lane.architecture == "arm"
        else "riscv-none-elf-gdb-py3"
    )
    for name in ("gdb-multiarch", specific, "gdb"):
        if (tools / name).is_file():
            return str(tools / name)
    raise ValueError("missing Python-enabled GDB; install it and run setup")


def doctor(lane: Lane, *, compatibility: bool = False) -> None:
    """Fail before fetching sources when the selected lane lacks tools."""
    env = environment()
    _probe(["uv", "--version"], env=env)
    build_tools(lane)
    if lane.qemu:
        binary = CACHE / "tools" / host_id() / "bin" / lane.qemu
        if not binary.is_file():
            raise ValueError(f"missing {binary}; install QEMU and run setup")
        _probe([str(binary), "--version"], env=env)
    args = [
        "bash",
        str(ROOT / "ci/checks/gdb-python.sh"),
        "--gdb",
        gdb_for(lane),
        "--architecture",
        lane.architecture,
    ]
    if compatibility:
        args += ["--major", "12", "--python", "3.10"]
    _probe(args, env=env)


def build_tools(lane: Lane) -> None:
    """Check compilation dependencies before preparing upstream sources."""
    env = environment()
    directory, prefix = compiler(lane)
    for tool in ("gcc", "objcopy", "readelf"):
        _probe([str(directory / (prefix + tool)), "--version"], env=env)
    if lane.variant != "eh-frame":
        _probe(["git", "--version"], env=env)
    if lane.rtos == "rtthread":
        if lane.version.startswith("3.1."):
            legacy = CACHE / "tools" / host_id() / "python2"
            if (
                not (legacy / "bin/python").is_file()
                or not (legacy / "bin/scons").is_file()
            ):
                raise ValueError(
                    "RT-Thread 3.1.x requires cached Python 2/SCons 3; use the qemu container"
                )
        else:
            _probe(["scons", "--version"], env=env)


def preflight(lanes: Iterable[Lane], *, compatibility: bool = False) -> None:
    """Verify each distinct tool requirement before a matrix starts building."""
    seen: set[tuple[str, str | None, str, str, str, bool]] = set()
    for lane in lanes:
        key = (
            lane.architecture,
            lane.qemu,
            lane.rtos,
            lane.version,
            lane.variant,
            compatibility,
        )
        if key in seen:
            continue
        seen.add(key)
        doctor(lane, compatibility=compatibility)


def sources(lane: Lane) -> Path:
    """Prepare a release checkout while serializing its shared cache entry."""
    with cache_lock(f"sources/{lane.rtos}/{lane.tag}", wait=True):
        return _sources(lane)


def _source_clean(source: Path) -> bool:
    """Reject changes, allowing Git false positives only for identical raw blobs."""
    status = subprocess.check_output(
        [
            "git",
            "-C",
            str(source),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        ],
    )
    for record in status.split(b"\0"):
        if not record:
            continue
        if record[:3] != b" M ":
            return False
        name = os.fsdecode(record[3:])
        entry = subprocess.check_output(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(source),
                "ls-tree",
                "-z",
                "HEAD",
                "--",
                name,
            ],
        )
        if not entry:
            return False
        mode, kind, expected = entry.split(b"\t", 1)[0].split()
        path = source / name
        if kind != b"blob" or mode not in {b"100644", b"100755"} or path.is_symlink():
            return False
        try:
            actual_mode = b"100755" if path.stat().st_mode & 0o111 else b"100644"
        except OSError:
            return False
        if mode != actual_mode:
            return False
        actual = subprocess.check_output(
            ["git", "-C", str(source), "hash-object", "--no-filters", "--", name],
        ).strip()
        if actual != expected:
            return False
    return True


def _sources(lane: Lane) -> Path:
    """Cache one immutable checkout per release, never a user working tree."""
    source = CACHE / "sources" / lane.rtos / lane.tag
    if not source.exists():
        source.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="fetch-", dir=source.parent))
        command(
            [
                "git",
                "clone",
                "--depth=1",
                "--branch",
                lane.tag,
                REPOSITORIES[lane.rtos],
                str(temporary),
            ],
            env=environment(),
            log=CACHE
            / "logs/sources"
            / lane.rtos
            / lane.tag
            / (temporary.name + ".log"),
        )
        temporary.rename(source)
    expected = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", f"refs/tags/{lane.tag}^{{commit}}"],
        text=True,
    ).strip()
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != expected or not _source_clean(source):
        raise ValueError(
            f"source cache modified: {source}; preserve it and restore a clean tag explicitly"
        )
    return source


def fingerprint(lane: Lane) -> str:
    """Hash fixture inputs and compiler identity, not host-dependent mtimes."""
    if lane.rtos == "bare":
        directory = ROOT / "ci" / "bare"
    else:
        directory = (
            ROOT / "ci" / ("freertos" if lane.rtos == "freertos" else "rt-thread")
        )
    digest = hashlib.sha256(lane.key.encode())
    paths = [
        p
        for p in directory.rglob("*")
        if p.is_file() and p.suffix in {".sh", ".c", ".h", ".s", ".S", ".ld", ".patch"}
    ]
    paths += [Path(__file__), ROOT / "ci/testing/matrix.py"]
    for path in sorted(paths):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    tool_dir, prefix = compiler(lane)
    identity = subprocess.check_output([str(tool_dir / (prefix + "gcc")), "--version"])
    digest.update(identity)
    return digest.hexdigest()


def fresh(lane: Lane, signature: str) -> bool:
    """Require every artifact and a matching successful-build manifest."""
    try:
        manifest = json.loads((lane.directory / "manifest.json").read_text())
        return manifest["fingerprint"] == signature and all(
            (lane.directory / name).stat().st_size > 0 for name in lane.artifacts
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def build(lane: Lane, *, rebuild: bool = False) -> None:
    """Build one cache cell while refusing concurrent use of that cell."""
    with lane_lock(lane):
        _build(lane, rebuild=rebuild)


@contextmanager
def lane_lock(lane: Lane) -> Iterator[None]:
    """Serialize firmware publication and readers across host processes."""
    with cache_lock(lane.key):
        yield


@contextmanager
def cache_lock(key: str, *, wait: bool = False) -> Iterator[None]:
    """Lock one cache resource; fail immediately unless waiting is requested."""
    path = CACHE / "locks" / (key + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ValueError(f"cache entry already in use: {key}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _build(lane: Lane, *, rebuild: bool = False) -> None:
    """Build in a new private directory; publish only complete artifact sets."""
    signature = fingerprint(lane)
    if not rebuild and fresh(lane, signature):
        print(f"[gdr-ci] reuse {lane.key}", flush=True)
        return
    build_tools(lane)
    parent = CACHE / "build" / host_id() / lane.key
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="build-", dir=parent))
    tool_dir, prefix = compiler(lane)
    env = environment()
    if lane.variant == "eh-frame":
        args = [
            "bash",
            str(ROOT / "ci/bare/eh-frame/build.sh"),
            str(work),
            str(tool_dir),
        ]
    elif lane.rtos == "freertos":
        args = [
            "bash",
            str(ROOT / "ci/freertos/build.sh"),
            "--kernel-dir",
            str(sources(lane)),
            "--tag",
            lane.tag,
            "--target",
            lane.target,
            "--variant",
            lane.variant,
            "--version",
            lane.version,
            "--build-dir",
            str(work),
            "--cache-dir",
            str(work / "installed"),
            "--toolchain-path",
            str(tool_dir),
        ]
    else:
        args = [
            "bash",
            str(ROOT / "ci/rt-thread/build.sh"),
            "--repo",
            str(sources(lane)),
            "--ref",
            lane.tag,
            "--target",
            lane.target,
            "--variant",
            lane.variant,
            "--build-dir",
            str(work / "source"),
            "--out-elf",
            str(work / "rtthread.elf"),
            "--out-bin",
            str(work / "rtthread.bin"),
            "--toolchain-path",
            str(tool_dir),
            "--toolchain-prefix",
            prefix,
            "--g3",
        ]
    command(args, env=env, log=work / "build.log")
    output = (
        work / "installed"
        if lane.rtos == "freertos" and lane.variant != "eh-frame"
        else work
    )
    if lane.rtos == "rtthread":
        (output / ".debug-macros").write_text("-g3 -gdwarf-4\n")
    for name in lane.artifacts:
        if not (output / name).is_file() or not (output / name).stat().st_size:
            raise ValueError(f"builder did not produce {output / name}")
    lane.directory.mkdir(parents=True, exist_ok=True)
    manifest = lane.directory / "manifest.json"
    manifest.unlink(missing_ok=True)
    for name in lane.artifacts:
        temporary = lane.directory / (name + ".new")
        shutil.copy2(output / name, temporary)
        temporary.replace(lane.directory / name)
    manifest.write_text(
        json.dumps(
            {"lane": lane.key, "fingerprint": signature, "build": str(work)}, indent=2
        )
        + "\n"
    )


def run(
    lane: Lane,
    pytest_args: list[str],
    *,
    rebuild: bool = False,
    compatibility: bool = False,
    check_tools: bool = True,
) -> None:
    """Run a selected lane while its firmware cache cell is locked."""
    with lane_lock(lane):
        _run(
            lane,
            pytest_args,
            rebuild=rebuild,
            compatibility=compatibility,
            check_tools=check_tools,
        )


def _run(
    lane: Lane,
    pytest_args: list[str],
    *,
    rebuild: bool = False,
    compatibility: bool = False,
    check_tools: bool = True,
) -> None:
    """Preflight, prepare firmware, and run pytest with isolated transport."""
    if check_tools:
        doctor(lane, compatibility=compatibility)
    _build(lane, rebuild=rebuild)
    with cache_lock(f"env/{host_id()}/{environment_id()}", wait=True):
        _pytest(lane, pytest_args)


def _pytest(lane: Lane, pytest_args: list[str]) -> None:
    """Synchronize the host environment and retain one pytest run's outputs."""
    env = environment()
    env.update(
        {
            "GDR_RTOS": lane.rtos,
            "GDR_QEMU_TARGET": lane.target,
            "GDR_VERSION": lane.version,
            "GDR_FIXTURE_VARIANT": lane.variant,
            "GDR_GDB": gdb_for(lane),
        }
    )
    command(
        ["uv", "sync", "--locked", "--group", "dev", "--python", sys.executable],
        env=env,
    )
    log_dir = CACHE / "logs" / host_id() / lane.key
    log_dir.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=log_dir))
    suite = {
        "eh-frame": "tests/integration/bare/test_eh_frame.py",
        "snapshot": "tests/integration/freertos/test_snapshot.py",
    }.get(lane.variant, f"tests/integration/{lane.rtos}")
    has_node = any(
        (ROOT / arg.split("::", 1)[0]).resolve().is_relative_to(ROOT / "tests")
        and (ROOT / arg.split("::", 1)[0]).exists()
        for arg in pytest_args
        if not arg.startswith("-")
    )
    suites = [suite]
    if lane.variant not in {"snapshot", "eh-frame"}:
        suites.append("tests/integration/test_panel_tables.py")
    args = [
        "uv",
        "run",
        "--no-sync",
        "pytest",
        *([] if has_node else suites),
        "-v",
        "--tb=short",
        "-o",
        f"cache_dir={CACHE / 'tmp/pytest-cache'}",
        "--basetemp",
        str(output / "tmp"),
        "--junitxml",
        str(output / "results.xml"),
        *pytest_args,
    ]
    print(f"[gdr-ci] results: {output}", flush=True)
    command(args, env=env, log=output / "pytest.log")
    if {"--collect-only", "--co", "--help", "--version"}.intersection(pytest_args):
        return
    try:
        report = ET.parse(output / "results.xml").getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(
            f"missing or invalid pytest report: {output / 'results.xml'}"
        ) from exc
    cases = list(report.iter("testcase"))
    if not any(case.find("skipped") is None for case in cases):
        raise ValueError(
            f"no tests executed for {lane.key}; see {output / 'results.xml'}"
        )
