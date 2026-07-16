#!/usr/bin/env bash
# Build every supported RT-Thread fixture for one QEMU target and run pytest.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <cortex-a9|rv64>" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
case "$1" in
    cortex-a9)
        export RT_THREAD_TARGET="cortex-a9"
        export GDR_QEMU_TARGET="cortex-a9"
        export RTOS_TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-${XPACK_ARM_TOOLCHAIN_PATH:?XPACK_ARM_TOOLCHAIN_PATH is required}}"
        export CROSS_TOOL_PREFIX="${CROSS_TOOL_PREFIX:-arm-none-eabi-}"
        refs=(v4.0.0 v4.0.5 v4.1.1)
        ;;
    rv64)
        export RT_THREAD_TARGET="rv64"
        export GDR_QEMU_TARGET="rv64"
        export RTOS_TOOLCHAIN_PATH="${RTOS_TOOLCHAIN_PATH:-${XPACK_RISCV_TOOLCHAIN_PATH:?XPACK_RISCV_TOOLCHAIN_PATH is required}}"
        export CROSS_TOOL_PREFIX="${CROSS_TOOL_PREFIX:-riscv64-unknown-elf-}"
        refs=(v4.0.4 v4.0.5 v4.1.0 v4.1.1)
        ;;
    *)
        echo "unknown target: $1" >&2
        exit 2
        ;;
esac

export GDR_GDB="${GDR_GDB:-gdb-multiarch}"
# Reason: keep local container runs from creating a Linux virtualenv in the mounted repo.
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/gdr-venv}"
SOURCE_REPO="${RT_THREAD_REPO:-https://github.com/RT-Thread/rt-thread.git}"
SOURCE_CACHE="${RT_THREAD_SOURCE_CACHE:-/tmp/rt-thread-build/xpack/source}"
if [[ ! -d "$SOURCE_CACHE" ]]; then
    mkdir -p "$(dirname "$SOURCE_CACHE")"
    git init --bare "$SOURCE_CACHE"
    git -C "$SOURCE_CACHE" remote add origin "$SOURCE_REPO"
fi
for ref in "${refs[@]}"; do
    if ! git -C "$SOURCE_CACHE" rev-parse --verify --quiet "refs/tags/$ref^{commit}" >/dev/null; then
        git -C "$SOURCE_CACHE" fetch --depth=1 --no-tags origin "refs/tags/$ref:refs/tags/$ref"
    fi
done
# Reason: each version clone reads the cached tag locally instead of GitHub.
export RT_THREAD_REPO="file://$SOURCE_CACHE"
uv sync --group dev

for ref in "${refs[@]}"; do
    if [[ "$RT_THREAD_TARGET" == "rv64" && "$ref" == "v4.1.1" ]]; then
        bsp="bsp/qemu-virt64-riscv"
    elif [[ "$RT_THREAD_TARGET" == "rv64" ]]; then
        bsp="bsp/qemu-riscv-virt64"
    else
        bsp="bsp/qemu-vexpress-a9"
    fi

    export RT_THREAD_REF="$ref"
    export GDR_RTTHREAD_VERSION="${ref#v}"
    # Reason: do not reuse cached SCons outputs produced by Debian toolchains.
    export BUILD_DIR="/tmp/rt-thread-build/xpack/$RT_THREAD_TARGET/$ref"
    export GDR_ELF_PATH="$BUILD_DIR/$bsp/rtthread.elf"
    if [[ "$RT_THREAD_TARGET" == "rv64" ]]; then
        export GDR_FIRMWARE_PATH="$BUILD_DIR/$bsp/rtthread.bin"
    else
        unset GDR_FIRMWARE_PATH
    fi
    bash "$SCRIPT_DIR/build-rtt.sh"
    uv run pytest tests/ -v --tb=short
done
