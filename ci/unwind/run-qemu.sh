#!/usr/bin/env bash
# Verify the standalone RV64 .eh_frame fixture with the selected toolchain.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
prefix="${XPACK_RISCV_TOOLCHAIN_PATH:+$XPACK_RISCV_TOOLCHAIN_PATH/}riscv-none-elf-"
export GDR_GDB="${GDR_GDB:-gdb-multiarch}"
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-/tmp/gdr-venv}"

for tool in gcc objcopy readelf; do
    "${prefix}${tool}" --version >/dev/null
done
qemu-system-riscv64 --version
"$GDR_GDB" --nx --quiet --batch \
    --ex 'set architecture riscv:rv64' \
    --ex 'python print("GDR_PYTHON_OK")'

cd "$root_dir"
uv sync --locked --group dev
uv run pytest tests/integration/test_unwind_eh_frame.py -v -ra
