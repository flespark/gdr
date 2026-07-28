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

podman build --platform "$PLATFORM" --file "$ROOT_DIR/ci/Dockerfile" --tag "$IMAGE_TAG" "$ROOT_DIR"
podman run "${podman_args[@]}" "$IMAGE_TAG" \
    bash -c '
        set -e
        RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
        CROSS_TOOL_PREFIX=arm-none-eabi- \
        bash ci/rt-thread/run-qemu-matrix.sh cortex-a9
        RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
        CROSS_TOOL_PREFIX=riscv64-unknown-elf- \
        bash ci/rt-thread/run-qemu-matrix.sh rv64
    '
