# Standalone unwind fixture

This fixture isolates `.eh_frame` from RTOS saved-context decoding. Coverage
and CI placement live in [testing](../../docs/testing.md#standalone-eh_frame-unwinding);
runtime rules and limitations live in [architecture](../../docs/architecture.md)
and [errata](../../docs/errata.md#task-backtraces).

## Tools and build

CI uses the xPack GNU RISC-V GCC 15.2.0-1 toolchain from
[`ci/Dockerfile`](../Dockerfile), `qemu-system-riscv64`, a Python-enabled
RV64-capable GDB, and the locked development dependencies.
`XPACK_RISCV_TOOLCHAIN_PATH` selects the directory containing
`riscv-none-elf-{gcc,objcopy,readelf}`; otherwise they must be on `PATH`.
`GDR_GDB` defaults to `gdb-multiarch`.

```bash
XPACK_RISCV_TOOLCHAIN_PATH=/path/to/toolchain/bin \
GDR_GDB=/path/to/python-enabled-gdb bash ci/unwind/run-qemu.sh
```

`run-qemu.sh` probes every required binary before pytest, preventing absent
tools from passing CI by skipping. `UV_PROJECT_ENVIRONMENT` selects the
virtualenv (default `/tmp/gdr-venv`). Pytest builds fresh artifacts in its
temporary directory; `--basetemp /path/to/output` can relocate them when
running pytest directly. No RTOS source checkout or fixture cache is needed.

`build-fixture.sh OUTPUT_DIRECTORY` produces `unwind.elf`, `unwind.bin` and
`frames.txt`. The assembly selects `.cfi_sections .eh_frame`; build flags are
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
