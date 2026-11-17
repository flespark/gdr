#!/usr/bin/env bash
# Build the standalone RV64 .eh_frame fixture into the supplied directory.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    echo "Usage: $0 OUTPUT_DIRECTORY TOOLCHAIN_BIN_DIRECTORY"
    exit 0
fi
[[ $# -eq 2 ]] || { echo "Usage: $0 OUTPUT_DIRECTORY TOOLCHAIN_BIN_DIRECTORY" >&2; exit 2; }
output="$1"
prefix="$2/riscv-none-elf-"
mkdir -p "$output"

"${prefix}gcc" -march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany \
    -g -nostdlib -nostartfiles -Wl,--no-relax \
    -T"$script_dir/linker.ld" "$script_dir/fixture.S" -o "$output/unwind.elf"
"${prefix}objcopy" -O binary "$output/unwind.elf" "$output/unwind.bin"
"${prefix}readelf" --debug-dump=frames "$output/unwind.elf" >"$output/frames.txt"

"${prefix}gcc" -march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany \
    -g -nostdlib -nostartfiles -Wl,--no-relax \
    -T"$script_dir/linker_hdr.ld" "$script_dir/hdr.S" -o "$output/unwind_hdr.elf"
"${prefix}objcopy" -O binary "$output/unwind_hdr.elf" "$output/unwind_hdr.bin"
"${prefix}readelf" --debug-dump=frames "$output/unwind_hdr.elf" >"$output/hdr_frames.txt"

"${prefix}gcc" -march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany \
    -g -nostdlib -nostartfiles -Wl,--no-relax \
    -T"$script_dir/linker.ld" "$script_dir/reloc.S" -o "$output/unwind_reloc.sym.elf"
"${prefix}objcopy" --rename-section .eh_frame_enc=.eh_frame \
    "$output/unwind_reloc.sym.elf" "$output/unwind_reloc.elf"
"${prefix}objcopy" -O binary "$output/unwind_reloc.elf" "$output/unwind_reloc.bin"
"${prefix}readelf" --debug-dump=frames "$output/unwind_reloc.elf" >"$output/reloc_frames.txt"

"${prefix}gcc" --version | head -1
echo "[gdr-ci] .eh_frame fixture: $output/unwind.elf"
