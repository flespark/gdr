"""Podman images, VM state and bind mounts rooted in the integration cache."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import time

from .cache import CACHE, ROOT, host_id, initialize
from .runner import command

_MACHINE = "gdr-ci"
_PODMAN_READY_ATTEMPTS = 3


def container_environment() -> dict[str, str]:
    """Use a dedicated Podman store without touching a user's default machine."""
    env = dict(os.environ)
    for key in (
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
        "CONTAINERS_STORAGE_CONF",
        "CONTAINERS_CONF",
        "DOCKER_HOST",
        "PODMAN_CONNECTIONS_CONF",
    ):
        env.pop(key, None)
    for key, directory in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        path = CACHE.resolve() / "containers" / host_id() / directory
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    runtime = CACHE.resolve() / "containers" / host_id() / "runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["TMPDIR"] = str(CACHE.resolve() / "tmp")
    return env


def build_arguments(profile: str, selected_platform: str) -> list[str]:
    """Select the pinned archive hashes matching the image architecture."""
    if profile in ("gdb12", "gdb-latest") and selected_platform != "linux/amd64":
        raise ValueError(f"the {profile} baseline is linux/amd64 only")
    if selected_platform == "linux/arm64":
        return [
            "--build-arg",
            "TARGETARCH=arm64",
            "--build-arg",
            "XPACK_ARCH=linux-arm64",
            "--build-arg",
            "XPACK_ARM_SHA256=67980c7990eba7bb7ffdf39699102effd70889f5ac427be19a8c8a6c5fab2972",
            "--build-arg",
            "XPACK_RISCV_SHA256=4e60e2a54c16385e4e2476d08240f857495d5a61609d97e1ee49f72875a6ec1e",
        ]
    return []


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _run_quiet(
    args: list[str], env: dict[str, str], *, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _machine_exists(env: dict[str, str]) -> bool:
    result = _run_quiet(["podman", "machine", "list", "--format", "{{.Name}}"], env)
    if result.returncode:
        raise RuntimeError(
            "podman machine list failed; run `podman machine list` for details"
        )
    return any(name.rstrip("*") == _MACHINE for name in result.stdout.split())


def _machine_state(env: dict[str, str]) -> str:
    result = _run_quiet(
        ["podman", "machine", "inspect", _MACHINE, "--format", "{{.State}}"], env
    )
    if result.returncode:
        raise RuntimeError(
            f"podman machine inspect {_MACHINE} failed; run `podman machine list` for details"
        )
    return result.stdout.strip()


def _podman_ready(env: dict[str, str]) -> bool:
    for attempt in range(_PODMAN_READY_ATTEMPTS):
        try:
            result = _run_quiet(["podman", "info"], env, timeout=15)
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            return True
        if attempt + 1 < _PODMAN_READY_ATTEMPTS:
            time.sleep(1)
    return False


def _init_machine(env: dict[str, str]) -> None:
    mounts = ["--volume", f"{ROOT}:{ROOT}"]
    if not CACHE.resolve().is_relative_to(ROOT):
        mounts += ["--volume", f"{CACHE.resolve()}:{CACHE.resolve()}"]
    command(["podman", "machine", "init", *mounts, _MACHINE], env=env)


def _start_machine(env: dict[str, str]) -> None:
    print(f"[gdr-ci] starting Podman machine {_MACHINE}", flush=True)
    command(["podman", "machine", "start", _MACHINE], env=env)


def _stop_machine(env: dict[str, str]) -> bool:
    try:
        result = _run_quiet(["podman", "machine", "stop", _MACHINE], env, timeout=60)
    except subprocess.TimeoutExpired:
        print(
            f"[gdr-ci] stopping Podman machine {_MACHINE} timed out",
            flush=True,
        )
        return False
    return result.returncode == 0


def _ensure_machine(env: dict[str, str], *, create: bool) -> None:
    """Start the CI machine, restarting it when its recorded state is stale."""
    if not _machine_exists(env):
        if not create:
            raise ValueError(
                f"Podman machine {_MACHINE} is not initialized; "
                "run: python3 ci/test.py container init"
            )
        _init_machine(env)
    state = _machine_state(env)
    if state == "running" and _podman_ready(env):
        return
    if state == "running":
        print(
            f"[gdr-ci] Podman machine {_MACHINE} reports running but is unreachable; "
            "restarting",
            flush=True,
        )
        if not _stop_machine(env) and _podman_ready(env):
            return
    try:
        _start_machine(env)
    except subprocess.CalledProcessError:
        if _podman_ready(env):
            return
        raise ValueError(
            f"Podman machine {_MACHINE} is running but unreachable and could not be "
            "restarted; restart the Podman machine or the host, then retry"
        ) from None
    if not _podman_ready(env):
        raise RuntimeError(
            f"Podman machine {_MACHINE} did not become reachable after start; "
            "check that the machine disk is mounted and writable, then retry"
        )


def container_command(
    action: str,
    profile: str,
    selected_platform: str | None,
    no_build: bool,
    forwarded: list[str],
    *,
    no_start: bool = False,
) -> None:
    """Initialize an isolated VM, stop it, or build/run one reproducible image."""
    if action == "stop" and not _is_macos():
        print(
            "[gdr-ci] CI uses native Podman on this host; no gdr-ci machine to stop",
            flush=True,
        )
        return
    initialize(prompt=True)
    env = container_environment()
    if action == "stop":
        if not _machine_exists(env):
            print(
                f"[gdr-ci] Podman machine {_MACHINE} is not initialized; nothing to stop",
                flush=True,
            )
            return
        if _machine_state(env) == "stopped":
            print(f"[gdr-ci] Podman machine {_MACHINE} is already stopped", flush=True)
            return
        if not _stop_machine(env) and _machine_state(env) != "stopped":
            raise ValueError(
                f"Podman machine {_MACHINE} did not stop; restart it or the host, "
                "then retry"
            )
        print(f"[gdr-ci] stopped Podman machine {_MACHINE}", flush=True)
        return
    if action == "init":
        if _is_macos():
            _ensure_machine(env, create=True)
        else:
            command(["podman", "info"], env=env)
        return
    selected_platform = selected_platform or (
        "linux/amd64"
        if profile in ("gdb12", "gdb-latest")
        else "linux/arm64"
        if platform.machine() in ("arm64", "aarch64")
        else "linux/amd64"
    )
    build_args = build_arguments(profile, selected_platform)
    if _is_macos() and not no_start:
        _ensure_machine(env, create=False)
    image = f"gdr-ci:{profile}-{selected_platform.rsplit('/', 1)[1]}"
    if not no_build:
        dockerfile_name = {
            "gdb12": "Dockerfile.gdb12",
            "gdb-latest": "Dockerfile.gdb-latest",
        }.get(profile, "Dockerfile")
        dockerfile = ROOT / "ci/containers" / dockerfile_name
        command(
            [
                "podman",
                "build",
                "--platform",
                selected_platform,
                "--file",
                str(dockerfile),
                *build_args,
                "--tag",
                image,
                str(dockerfile.parent),
            ],
            env=env,
        )
    elif subprocess.run(
        ["podman", "image", "exists", image], env=env, check=False
    ).returncode:
        raise ValueError(f"image missing: {image}; run container build")
    if action == "build":
        return
    default_group = profile if profile in ("gdb12", "gdb-latest") else "smoke"
    inner = forwarded or ["matrix", default_group]
    mounts = ["--volume", f"{ROOT}:{ROOT}"]
    if not CACHE.resolve().is_relative_to(ROOT):
        mounts += ["--volume", f"{CACHE.resolve()}:{CACHE.resolve()}"]
    command(
        [
            "podman",
            "run",
            "--rm",
            "--platform",
            selected_platform,
            *mounts,
            "--workdir",
            str(ROOT),
            image,
            "python3",
            "ci/test.py",
            *inner,
        ],
        env=env,
    )
