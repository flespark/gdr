# Isolated RV64 `.eh_frame` fixture

This fixture isolates `.eh_frame` from RTOS saved-context decoding. Coverage
and CI placement live in [testing](../../../../docs/testing.md#isolated-eh_frame-variant);
runtime rules and limitations live in [architecture](../../../../docs/architecture.md)
and [errata](../../../../docs/errata.md#task-backtraces).

## Tools and build

This is the `freertos/qemu-virt-rv64/11.1.0/eh-frame` cell in the
[shared integration CLI](../../../README.md). It requires the RISC-V
compiler, `qemu-system-riscv64`, and Python-enabled RV64-capable GDB.
The shared [container](../../../containers/Dockerfile) pins the compiler.

```bash
python3 ci/test.py run freertos qemu-virt-rv64 11.1.0 eh-frame
```

The runner checks tools and prepares artifacts before pytest, retaining
build/test logs and a firmware manifest in the shared cache. No RTOS source
checkout is needed. The internal
`build.sh OUTPUT_DIRECTORY TOOLCHAIN_BIN_DIRECTORY` produces `unwind.elf`,
`unwind.bin` and `frames.txt`. The assembly selects `.cfi_sections .eh_frame`; build flags are
`-march=rv64imac_zicsr -mabi=lp64 -mcmodel=medany -g -nostdlib -nostartfiles
-Wl,--no-relax`. The linker places code at `0x80000000` and reserves a
4 KiB stack. QEMU boots the binary with `-M virt -cpu rv64 -bios`; GDB loads
the separate ELF and controls execution only inside the test harness.

GNU as emits a data alignment of -4 for this fixture; the explicit
`DW_CFA_offset_extended` factor 2 therefore addresses CFA-8.
`frames.txt` records the assembler's emitted CIE/FDE rules for inspection.

## Deterministic checkpoints

The ordinary checkpoint is inside `cfi_checkpoint`, with the expected chain
`cfi_checkpoint -> cfi_leaf -> cfi_middle -> cfi_root`. The signal entry
loads a synthetic exact PC in x5 and jumps to `cfi_signal_checkpoint`; the
recovered address is the first byte of `cfi_signal_resume`. It is not a
hardware interrupt fixture.

Damage tests replace only the parser's in-memory ELF section bytes, not
target memory: remove `.eh_frame`, truncate its length header, or change
both used `zR`/return-column-1 CIE encodings from `pcrel` to `textrel` with no
base. Expected stops are `no-unwind-info`, `cfi-truncated-length` and
`cfi-fde-pointer-unavailable`, respectively.
