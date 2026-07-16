#!/usr/bin/env bash
# Reproduce CNB's amd64 QEMU matrices in a local Podman container.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="${GDR_CI_IMAGE:-gdr-ci:xpack}"
PLATFORM="${PODMAN_PLATFORM:-linux/amd64}"

podman build --platform "$PLATFORM" --file "$ROOT_DIR/ci/Dockerfile" --tag "$IMAGE_TAG" "$ROOT_DIR"
podman run --rm --platform "$PLATFORM" \
    --volume "$ROOT_DIR:/workspace" \
    --workdir /workspace \
    "$IMAGE_TAG" \
    bash -c '
        RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
        CROSS_TOOL_PREFIX=arm-none-eabi- \
        bash ci/rt-thread/run-qemu-matrix.sh cortex-a9
        RTOS_TOOLCHAIN_PATH=/opt/xpack-riscv-none-elf-gcc-15.2.0-1/bin \
        CROSS_TOOL_PREFIX=riscv64-unknown-elf- \
        bash ci/rt-thread/run-qemu-matrix.sh rv64
    '
