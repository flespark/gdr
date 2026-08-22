#!/usr/bin/env bash
# Build and run the Ubuntu 22.04 / GDB 12 / Python 3.10 Cortex-A9 smoke test.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_TAG="${GDR_GDB12_IMAGE:-gdr-ci:gdb12}"
# xPack's pinned ARM toolchain is the linux-x64 archive used by CNB's amd64
# runner. Use the same platform locally, including on Apple Silicon.
PLATFORM="${PODMAN_PLATFORM:-linux/amd64}"

# Keep the Podman machine, image store, and build cache off the system disk
# when callers provide external XDG roots (for example, a USB drive on macOS).
if [[ -n "${PODMAN_XDG_CONFIG_HOME:-}" ]]; then
    export XDG_CONFIG_HOME="$PODMAN_XDG_CONFIG_HOME"
fi
if [[ -n "${PODMAN_XDG_DATA_HOME:-}" ]]; then
    export XDG_DATA_HOME="$PODMAN_XDG_DATA_HOME"
fi

podman build --platform "$PLATFORM" --file "$ROOT_DIR/ci/Dockerfile.gdb12" \
    --tag "$IMAGE_TAG" "$ROOT_DIR"
podman run --rm --platform "$PLATFORM" \
    --volume "$ROOT_DIR:/workspace" \
    --workdir /workspace \
    --env GDR_GDB=gdb-multiarch \
    --env GDR_EXPECTED_GDB_MAJOR=12 \
    --env GDR_EXPECTED_EMBEDDED_PYTHON=3.10 \
    --env RT_THREAD_REFS=v4.0.5 \
    --env RTOS_TOOLCHAIN_PATH=/opt/xpack-arm-none-eabi-gcc-15.2.1-1.1/bin \
    --env CROSS_TOOL_PREFIX=arm-none-eabi- \
    "$IMAGE_TAG" bash ci/rt-thread/run-qemu-matrix.sh cortex-a9
