# Isolated RV64 `.eh_frame` fixture

This fixture isolates `.eh_frame` from RTOS saved-context decoding. Coverage
and CI placement live in [testing](../../../docs/testing.md#isolated-eh_frame-variant);
runtime rules and limitations live in [architecture](../../../docs/architecture.md)
and [errata](../../../docs/errata.md#task-backtraces).

## Tools and build

This is the `bare/qemu-virt-rv64/none/eh-frame` cell in the
[shared integration CLI](../../README.md). It requires the RISC-V
compiler, `qemu-system-riscv64`, and Python-enabled RV64-capable GDB.
The shared [container](../containers/Dockerfile) pins the compiler.

```bash
python3 ci/test.py run bare qemu-virt-rv64 none eh-frame
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

## Expression rules

The `cfi_expr_*` chain is emitted entirely by GNU as through `.cfi_escape`;
`frames.txt` shows the assembler printing `DW_CFA_def_cfa_expression`,
`DW_CFA_expression: r1 (ra)` and `DW_CFA_val_expression: r128`.

- `cfi_expr_leaf` computes CFA from `DW_OP_breg2 32` and the RA save address
  from `DW_OP_lit24; DW_OP_minus` (CFA-24).
- `cfi_expr_checkpoint` uses a virtual return column whose
  `DW_OP_drop; DW_OP_breg2 8; DW_OP_deref` value expression produces the
  saved caller PC directly. The driver must not dereference that result
  again.

## Deterministic checkpoints

The ordinary checkpoint is inside `cfi_checkpoint`, with the expected chain
`cfi_checkpoint -> cfi_leaf -> cfi_middle -> cfi_root`. The expression entry
sets `sp` and calls the `cfi_expr_*` chain; `cfi_expr_checkpoint_ready` is
reached after the checkpoint prologue and yields
`cfi_expr_checkpoint -> cfi_expr_leaf -> cfi_expr_middle -> cfi_expr_root`.
The signal entry loads a synthetic exact PC in x5 and jumps to
`cfi_signal_checkpoint`; the recovered address is the first byte of
`cfi_signal_resume`. It is not a hardware interrupt fixture.

Damage tests replace only the parser's in-memory ELF section bytes, not
target memory: remove `.eh_frame`, truncate its length header, change both
used `zR`/return-column-1 CIE encodings from `pcrel` to `textrel` with no
base, or replace the first `val_expression` operator with the DWARF-forbidden
`DW_OP_call_frame_cfa`. Expected stops are `no-unwind-info`,
`cfi-truncated-length`, `cfi-fde-pointer-unavailable` and
`cfi-expression-forbidden`, respectively.
