#!/usr/bin/env bash
# Reproduce CNB's QEMU matrices locally in a Podman container.
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

# Reuse a prebuilt image when GDR_CI_SKIP_BUILD is set: the xPack toolchains
# (roughly 300MB each) download slowly, and an uncommitted Dockerfile change
# invalidates the build cache and forces a redownload.
if [[ -n "${GDR_CI_SKIP_BUILD:-}" ]] && podman image exists "$IMAGE_TAG"; then
    echo "[gdr-ci] reusing existing image $IMAGE_TAG (GDR_CI_SKIP_BUILD)"
else
    # Seed ci/gdr-xpack with pre-downloaded archives (host-side cache, faster
    # than the VM's GitHub download). The Dockerfile falls back to curl for
    # CNB where no local cache exists. xPack arm64+x64 archives share one dir.
    archive_dir="${GDR_CI_XPACK_DIR:-/Volumes/PS3000/gdr-xpack-archives}"
    if [[ -d "$archive_dir" ]]; then
        mkdir -p "$ROOT_DIR/ci/gdr-xpack"
        for archive in \
            "xpack-arm-none-eabi-gcc-15.2.1-1.1-${XPACK_ARCH}.tar.gz" \
            "xpack-riscv-none-elf-gcc-15.2.0-1-${XPACK_ARCH}.tar.gz"; do
            if [[ -s "$archive_dir/$archive" ]]; then
                echo "[gdr-ci] seeding $archive from $archive_dir"
                cp "$archive_dir/$archive" "$ROOT_DIR/ci/gdr-xpack/$archive"
            fi
        done
    fi
    podman build --platform "$PLATFORM" --file "$ROOT_DIR/ci/Dockerfile" \
        "${build_args[@]}" --tag "$IMAGE_TAG" "$ROOT_DIR"
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

        if [[ "$lanes" == *all* || "$lanes" == *rtthread* ]]; then
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/rt-thread/run-qemu-matrix.sh cortex-a9
            RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
            bash ci/rt-thread/run-qemu-matrix.sh rv64
        fi

        if [[ "$lanes" == *all* || "$lanes" == *freertos* ]]; then
            # Reason: the FreeRTOS lane installs freshly built fixtures into
            # its cache root, so it must target a container-writable
            # directory -- the host FREERTOS_FIXTURE_CACHE is mounted.
            export FREERTOS_FIXTURE_CACHE=/tmp/gdr-freertos-cache
            # snapshot: file-only ELF, same matrix runner as the live cells.
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an385 10.4.6 snapshot
            # config-scope: the CubeL4 board carries the 14-variant matrix.
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh b-l475e-iot01a 10.3.1 base
            # version-scope: one kernel-direct cell per board.
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an385 11.1.0 base
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.3.1 smp
            RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
            bash ci/freertos/run-qemu-matrix.sh mps2-an521 11.3.1 mpu
            RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
            bash ci/freertos/run-qemu-matrix.sh qemu-virt-rv64 11.1.0 rv64
        fi
    '
