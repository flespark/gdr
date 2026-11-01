#!/usr/bin/env bash
# Build the standalone RV64 .eh_frame fixture into the supplied directory.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
output="${1:?usage: build-fixture.sh OUTPUT_DIRECTORY}"
prefix="${XPACK_RISCV_TOOLCHAIN_PATH:+$XPACK_RISCV_TOOLCHAIN_PATH/}riscv-none-elf-"
mkdir -p "$output"

"${prefix}gcc" -march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany \
    -g -nostdlib -nostartfiles -Wl,--no-relax \
    -T"$script_dir/linker.ld" "$script_dir/fixture.S" -o "$output/unwind.elf"
"${prefix}objcopy" -O binary "$output/unwind.elf" "$output/unwind.bin"
"${prefix}readelf" --debug-dump=frames "$output/unwind.elf" >"$output/frames.txt"
"${prefix}gcc" --version | head -1
echo "[gdr-ci] .eh_frame fixture: $output/unwind.elf"
