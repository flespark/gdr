#!/usr/bin/env bash
# Build and run the Phase 1 FreeRTOS QEMU closed-loop smoke test.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD_DIR="${BUILD_DIR:-/tmp/gdr-freertos-build}"

export GDR_RTOS=freertos
export GDR_VERSION="${GDR_VERSION:-10.3.1}"
export GDR_QEMU_TARGET="${GDR_QEMU_TARGET:-b-l475e-iot01a}"
export GDR_GDB="${GDR_GDB:-gdb-multiarch}"
export GDR_ELF_PATH="${GDR_ELF_PATH:-$BUILD_DIR/freertos_b_l475e_iot01a.elf}"
export GDR_FIRMWARE_PATH="${GDR_FIRMWARE_PATH:-$GDR_ELF_PATH}"
export OUT_ELF="$GDR_ELF_PATH"
export OUT_BIN="${OUT_BIN:-$BUILD_DIR/freertos_b_l475e_iot01a.bin}"

bash "$REPO_ROOT/ci/check-gdb-python.sh"
bash "$SCRIPT_DIR/build-freertos.sh"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/gdr-venv}"
uv sync --group dev
uv run pytest tests/test_freertos_boot.py -v --tb=short
