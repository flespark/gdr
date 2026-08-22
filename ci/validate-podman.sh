#!/usr/bin/env bash
# Reproduce CNB's amd64 QEMU matrices in a local Podman container.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="${GDR_CI_IMAGE:-gdr-ci:xpack}"
PLATFORM="${PODMAN_PLATFORM:-linux/amd64}"

# Keep the Podman machine, image store, and build cache off the system disk
# when callers provide external XDG roots (for example, a USB drive on macOS).
if [[ -n "${PODMAN_XDG_CONFIG_HOME:-}" ]]; then
    export XDG_CONFIG_HOME="$PODMAN_XDG_CONFIG_HOME"
fi
if [[ -n "${PODMAN_XDG_DATA_HOME:-}" ]]; then
    export XDG_DATA_HOME="$PODMAN_XDG_DATA_HOME"
fi

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
    podman build --platform "$PLATFORM" --file "$ROOT_DIR/ci/Dockerfile" --tag "$IMAGE_TAG" "$ROOT_DIR"
fi
podman run "${podman_args[@]}" "$IMAGE_TAG" \
    bash -c '
        set -e
        RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
        bash ci/rt-thread/run-qemu-matrix.sh cortex-a9
        RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
        bash ci/rt-thread/run-qemu-matrix.sh rv64
    '
