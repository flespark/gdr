"""Podman images, VM state and bind mounts rooted in the integration cache."""

from __future__ import annotations

import os
import platform
import subprocess
import sys

from .cache import CACHE, ROOT, host_id, initialize
from .runner import command


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
    if profile == "gdb12" and selected_platform != "linux/amd64":
        raise ValueError("the GDB 12 baseline is linux/amd64 only")
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


def container_command(
    action: str,
    profile: str,
    selected_platform: str | None,
    no_build: bool,
    forwarded: list[str],
) -> None:
    """Initialize an isolated VM or build/run one reproducible test image."""
    selected_platform = selected_platform or (
        "linux/amd64"
        if profile == "gdb12"
        else "linux/arm64"
        if platform.machine() in ("arm64", "aarch64")
        else "linux/amd64"
    )
    build_args = build_arguments(profile, selected_platform)
    initialize(prompt=True)
    env = container_environment()
    if action == "init":
        if sys.platform == "darwin":
            probe = subprocess.run(
                ["podman", "machine", "inspect", "gdr-ci", "--format", "{{.State}}"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode:
                mounts = ["--volume", f"{ROOT}:{ROOT}"]
                if not CACHE.resolve().is_relative_to(ROOT):
                    mounts += ["--volume", f"{CACHE.resolve()}:{CACHE.resolve()}"]
                command(["podman", "machine", "init", *mounts, "gdr-ci"], env=env)
            if probe.returncode or probe.stdout.strip() != "running":
                command(["podman", "machine", "start", "gdr-ci"], env=env)
        else:
            command(["podman", "info"], env=env)
        return
    image = f"gdr-ci:{profile}-{selected_platform.rsplit('/', 1)[1]}"
    if not no_build:
        dockerfile = (
            ROOT
            / "ci/containers"
            / ("Dockerfile.gdb12" if profile == "gdb12" else "Dockerfile")
        )
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
    inner = forwarded or ["matrix", "gdb12" if profile == "gdb12" else "smoke"]
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
