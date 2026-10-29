#!/usr/bin/env bash
# Reproduce CNB's QEMU matrices locally in a Podman container.
#
# Optional external workspace:
#   GDR_CI_WORK_DIR=/path/on/external/disk
# mounts that host path into the container and puts FreeRTOS/RT-Thread build
# trees, kernel caches, fixtures and the virtualenv there instead of /tmp.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="${GDR_CI_IMAGE:-gdr-ci:xpack}"

# Keep the Podman machine, image store, and build cache off the system disk
# when callers provide external XDG roots (for example, a USB drive on macOS).
if [[ -n "${PODMAN_XDG_CONFIG_HOME:-}" ]]; then
    export XDG_CONFIG_HOME="$PODMAN_XDG_CONFIG_HOME"
fi
if [[ -n "${PODMAN_XDG_DATA_HOME:-}" ]]; then
    export XDG_DATA_HOME="$PODMAN_XDG_DATA_HOME"
fi

# Native architecture by default so xPack toolchains, debian qemu/gdb and the
# RTOS compiler run as fast as on CNB's amd64 runners. PODMAN_PLATFORM still
# forces an explicit platform (e.g. linux/amd64) and the matching build args
# are picked below. The RTOS targets (Cortex-A9 / RV64) are cross-compiled, so
# fixtures are identical regardless of the image architecture.
case "${PODMAN_PLATFORM:-$(uname -m)}" in
linux/arm64 | arm64 | aarch64)
    PLATFORM="linux/arm64"
    IMAGE_TAG="${GDR_CI_IMAGE:-gdr-ci:xpack-arm64}"
    XPACK_ARCH="linux-arm64"
    build_args=(--build-arg XPACK_ARCH=linux-arm64
        --build-arg XPACK_ARM_SHA256=67980c7990eba7bb7ffdf39699102effd70889f5ac427be19a8c8a6c5fab2972
        --build-arg XPACK_RISCV_SHA256=4e60e2a54c16385e4e2476d08240f857495d5a61609d97e1ee49f72875a6ec1e)
    ;;
linux/amd64 | amd64 | x86_64)
    PLATFORM="linux/amd64"
    IMAGE_TAG="${GDR_CI_IMAGE:-gdr-ci:xpack}"
    XPACK_ARCH="linux-x64"
    build_args=()
    ;;
*)
    echo "unknown platform: ${PODMAN_PLATFORM:-$(uname -m)}" >&2
    exit 2
    ;;
esac

podman_args=(
    --rm
    --platform "$PLATFORM"
    --volume "$ROOT_DIR:/workspace"
    --workdir /workspace
    --env "GDR_VALIDATE_LANES=${GDR_VALIDATE_LANES:-all}"
)
if [[ -n "${RT_THREAD_REPO:-}" ]]; then
    podman_args+=(--env "RT_THREAD_REPO=$RT_THREAD_REPO")
fi
if [[ -n "${RT_THREAD_SOURCE_DIR:-}" ]]; then
    podman_args+=(--volume "$RT_THREAD_SOURCE_DIR:/rt-thread-source:ro")
fi
# Share a local RT-Thread git cache so the container does not re-fetch tags from
# GitHub (VM networking is slow and intermittently drops). The bare mirror cache
# is fetched with tags already present, so run-qemu-matrix keeps it out of the
# GitHub path. Note: prepare_source_cache needs a writable cache to run `git
# fetch`/`git init --bare`, so keep this mount writable.
if [[ -n "${RT_THREAD_SOURCE_CACHE:-}" ]]; then
    podman_args+=(
        --env "RT_THREAD_SOURCE_CACHE=$RT_THREAD_SOURCE_CACHE"
        --volume "$RT_THREAD_SOURCE_CACHE:$RT_THREAD_SOURCE_CACHE"
    )
fi
# Collect freshly built fixtures into a host directory (writable, unlike the
# read-only RT_THREAD_FIXTURE_CACHE). Laid out as <target>/<version>/, matching
# the cache convention so a later run can set RT_THREAD_FIXTURE_CACHE and skip
# recompilation.
if [[ -n "${RT_THREAD_FIXTURE_COLLECT_DIR:-}" ]]; then
    podman_args+=(
        --env "RT_THREAD_FIXTURE_COLLECT_DIR=$RT_THREAD_FIXTURE_COLLECT_DIR"
        --volume "$RT_THREAD_FIXTURE_COLLECT_DIR:$RT_THREAD_FIXTURE_COLLECT_DIR"
    )
fi
if [[ -n "${RT_THREAD_FIXTURE_CACHE:-}" ]]; then
    podman_args+=(
        --env "RT_THREAD_FIXTURE_CACHE=$RT_THREAD_FIXTURE_CACHE"
        --volume "$RT_THREAD_FIXTURE_CACHE:$RT_THREAD_FIXTURE_CACHE:ro"
    )
fi
if [[ -n "${FREERTOS_FIXTURE_CACHE:-}" ]]; then
    podman_args+=(
        --env "FREERTOS_FIXTURE_CACHE=$FREERTOS_FIXTURE_CACHE"
        --volume "$FREERTOS_FIXTURE_CACHE:$FREERTOS_FIXTURE_CACHE:ro"
    )
fi

# Writable build/fixture workspace.  When the caller names an external disk
# path, mount it at the same path and give every builder a subdirectory there
# instead of falling back to the container's /tmp.  The path is deliberately
# not created inside the repository, so no generated archive can be committed.
if [[ -n "${GDR_CI_WORK_DIR:-}" ]]; then
    mkdir -p "$GDR_CI_WORK_DIR"
    podman_args+=(
        --env "GDR_CI_WORK_DIR=$GDR_CI_WORK_DIR"
        --volume "$GDR_CI_WORK_DIR:$GDR_CI_WORK_DIR"
    )
fi

# Reuse a prebuilt image when GDR_SKIP_DOCKER_BUILD is set: the xPack toolchains
# (roughly 300MB each) download slowly, and an uncommitted Dockerfile change
# invalidates the build cache and forces a redownload.
if [[ -n "${GDR_SKIP_DOCKER_BUILD:-}" ]] && podman image exists "$IMAGE_TAG"; then
    echo "[gdr-ci] reusing existing image $IMAGE_TAG (GDR_SKIP_DOCKER_BUILD)"
else
    image_context="$(mktemp -d "${GDR_CI_WORK_DIR:-${TMPDIR:-/tmp}}/gdr-image-context.XXXXXX")"
    trap 'rm -rf "$image_context"' EXIT
    mkdir -p "$image_context/ci/gdr-xpack"
    cp "$ROOT_DIR/ci/Dockerfile" "$image_context/ci/Dockerfile"
    cp "$ROOT_DIR/ci/gdr-xpack/.gitkeep" "$image_context/ci/gdr-xpack/.gitkeep"
    archive_dir="${GDR_CI_XPACK_DIR:-/Volumes/PS3000/gdr-xpack-archives}"
    if [[ -d "$archive_dir" ]]; then
        for archive in \
            "xpack-arm-none-eabi-gcc-15.2.1-1.1-${XPACK_ARCH}.tar.gz" \
            "xpack-riscv-none-elf-gcc-15.2.0-1-${XPACK_ARCH}.tar.gz"; do
            if [[ -s "$archive_dir/$archive" ]]; then
                echo "[gdr-ci] seeding $archive from $archive_dir"
                cp "$archive_dir/$archive" "$image_context/ci/gdr-xpack/$archive"
            fi
        done
    fi
    podman build --platform "$PLATFORM" --file "$image_context/ci/Dockerfile" \
        "${build_args[@]}" --tag "$IMAGE_TAG" "$image_context"
fi
podman run "${podman_args[@]}" "$IMAGE_TAG" \
    bash -c '
        set -e
        # What to run inside the container.  Default: every CNB lane
        # (RT-Thread both targets; FreeRTOS snapshot plus one representative
        # cell per live board), so the local reproducer can never drift from
        # CI.  GDR_VALIDATE_LANES="rtthread:freertos" or a narrower list
        # opts into a fast subset.
        lanes="${GDR_VALIDATE_LANES:-all}"

        if [[ -n "${GDR_CI_WORK_DIR:-}" ]]; then
            mkdir -p "$GDR_CI_WORK_DIR"
            export FREERTOS_BUILD_ROOT="$GDR_CI_WORK_DIR/freertos-build"
            export FREERTOS_KERNEL_CACHE="$GDR_CI_WORK_DIR/freertos-kernel-source"
            export RT_THREAD_BUILD_ROOT="$GDR_CI_WORK_DIR/rt-thread-build"
            export UV_PROJECT_ENVIRONMENT="$GDR_CI_WORK_DIR/venv"
            mkdir -p "$FREERTOS_BUILD_ROOT" "$RT_THREAD_BUILD_ROOT"
        fi

        if [[ "$lanes" == *all* || "$lanes" == *rtthread* ]]; then
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/rt-thread/run-qemu-matrix.sh cortex-a9
            RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
            bash ci/rt-thread/run-qemu-matrix.sh rv64
        fi

        if [[ "$lanes" == *all* || "$lanes" == *freertos* ]]; then
            # NOTE: the FreeRTOS lane installs freshly built fixtures into
            # its cache root, so it must target a container-writable
            # directory -- the host FREERTOS_FIXTURE_CACHE is mounted read-only.
            # Prefer the caller external workspace; otherwise keep the
            # historical /tmp fallback.  Seed the writable copy from the host
            # cache so current fixtures are reused instead of rebuilt; cp -a
            # keeps the mtimes the runner compares against the sources.
            seed="${FREERTOS_FIXTURE_CACHE:-}"
            if [[ -n "${GDR_CI_WORK_DIR:-}" ]]; then
                export FREERTOS_FIXTURE_CACHE="$GDR_CI_WORK_DIR/freertos"
            else
                export FREERTOS_FIXTURE_CACHE=/tmp/gdr-freertos-cache
            fi
            if [[ -n "$seed" && "$seed" != "$FREERTOS_FIXTURE_CACHE" && -d "$seed" ]]; then
                mkdir -p "$FREERTOS_FIXTURE_CACHE"
                cp -a "$seed/." "$FREERTOS_FIXTURE_CACHE/"
            fi
            # config axis: 10.3.1 representative cell (the 14-variant matrix
            # runs in CI on the same board).
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an385 10.3.1 base
            # version axis: one kernel-direct cell per board.
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an385 11.1.0 base
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.3.1 smp
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.3.1 mpu
            RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
            bash ci/freertos/run-qemu-matrix.sh qemu-virt-rv64 11.1.0 rv64
            # Cortex-M0 and RV32 lanes, the other two first-version targets.
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh microbit 11.1.0 full
            RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
            bash ci/freertos/run-qemu-matrix.sh qemu-virt-rv32 11.1.0 base
            # snapshot: file-only ELF for the data-corruption negatives,
            # same matrix runner and kernel source as the other an521 cells.
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.1.0 snapshot
        fi
    '
