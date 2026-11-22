"""Unit tests for ELF/DWARF source-path inference, expansion, and shortening."""

from __future__ import annotations

import shutil
import struct
import subprocess

import pytest

from core import gdb_bridge as bridge
from core.formatting import SourceLoc, format_table
from core.source_paths import (
    SourcePathResolver,
    collect_local_source_files,
    format_source_location,
    infer_elf_prefix_mappings,
    normalize_path,
    relative_path,
    scan_elf_source_info,
    strip_after_last_parent,
)


def _build_synthetic_elf(sections: dict[str, bytes], *, endian: str = "<") -> bytes:
    """Construct a minimal ELF32 binary with the given named PROGBITS sections."""
    shstrtab = bytearray(b"\x00")
    entries: list[tuple[int, int, int]] = [(0, 0, 0)]
    payload_blobs: list[bytes] = []
    # Reserve space for ehdr (52) + shdrs ((len(sections)+2)*40)
    num_sections = len(sections) + 2
    shoff = 52
    cursor = shoff + num_sections * 40
    for name, content in sections.items():
        name_off = len(shstrtab)
        shstrtab.extend(name.encode("ascii") + b"\x00")
        entries.append((name_off, cursor, len(content)))
        payload_blobs.append(content)
        cursor += len(content)
    shstr_off = len(shstrtab)
    shstrtab.extend(b".shstrtab\x00")
    shstrtab_bytes = bytes(shstrtab)
    entries.append((shstr_off, cursor, len(shstrtab_bytes)))
    payload_blobs.append(shstrtab_bytes)

    ei_data = 1 if endian == "<" else 2
    e_ident = b"\x7fELF" + bytes((1, ei_data, 1)) + b"\x00" * 9
    ehdr = struct.pack(
        f"{endian}16sHHIIIIIHHHHHH",
        e_ident,
        2,  # ET_EXEC
        40,  # EM_ARM
        1,
        0,
        0,
        shoff,
        0,
        52,
        0,
        0,
        40,
        num_sections,
        num_sections - 1,
    )
    shdrs = bytearray()
    for name_off, off, size in entries:
        sh_type = 0 if (off == 0 and size == 0) else 1
        if name_off == shstr_off:
            sh_type = 3
        shdrs.extend(
            struct.pack(
                f"{endian}10I",
                name_off,
                sh_type,
                0,
                0,
                off,
                size,
                0,
                0,
                1,
                0,
            )
        )
    return ehdr + bytes(shdrs) + b"".join(payload_blobs)


def _build_debug_line_v4(dirs: list[str], files: list[tuple[str, int]]) -> bytes:
    """Build one minimal DWARF-4 ``.debug_line`` compilation unit header."""
    header_body = bytearray()
    header_body.append(2)  # minimum_instruction_length
    header_body.append(1)  # maximum_operations_per_instruction (v4)
    header_body.append(1)  # default_is_stmt
    header_body.extend(b"\xfb\x0e\x0d")  # line_base=-5, line_range=14, opcode_base=13
    header_body.extend(bytes([0, 1, 1, 1, 1, 0, 0, 0, 1, 0, 0, 1]))
    for d in dirs:
        header_body.extend(d.encode("utf-8") + b"\x00")
    header_body.append(0)
    for fname, dir_idx in files:
        header_body.extend(fname.encode("utf-8") + b"\x00")
        header_body.extend(bytes((dir_idx, 0, 0)))
    header_body.append(0)

    unit_body = bytearray()
    unit_body.extend(struct.pack("<H", 4))  # version
    unit_body.extend(struct.pack("<I", len(header_body)))
    unit_body.extend(header_body)
    return struct.pack("<I", len(unit_body)) + bytes(unit_body)


def test_relative_path_and_format_source_location_shorten_under_roots():
    roots = ("/ws/app", "/ws")
    assert relative_path("/ws/app/src/main.c", roots) == "src/main.c"
    assert relative_path("/ws/lib/util.c", roots) == "lib/util.c"
    assert relative_path("/opt/sdk/libc.c", roots) == "/opt/sdk/libc.c"
    assert (
        format_source_location(
            SourceLoc("/ws/app/portable/port.c", 42, "/ws/app/portable/port.c"), roots
        )
        == "portable/port.c:42"
    )
    assert (
        format_source_location(SourceLoc("/ci/build/src/a.c", 7, "src/a.c"), roots)
        == "src/a.c:7"
    )


def test_normalize_and_strip_after_last_parent():
    assert normalize_path(r"D:\ws\sdk\app\..\..\ext\hdr.h") == "D:/ws/ext/hdr.h"
    assert strip_after_last_parent("/a/b/c/../../../d/e.c") == "+d/e.c"
    assert strip_after_last_parent(r"D:\ws\build\..\..\ext\hdr.h") == "+ext/hdr.h"
    assert strip_after_last_parent("../../foo/bar.c") == "+foo/bar.c"
    assert strip_after_last_parent("no/parent/here.c") is None
    assert strip_after_last_parent("../..") is None


def test_fuzzy_prefix_mapping_remote_posix_and_windows_sdk(tmp_path):
    local_sdk = tmp_path / "freertos-local"
    for rel in (
        "tasks.c",
        "portable/GCC/ARM_CM33/port.c",
        "demos/blinky/main.c",
        "gdr-fixture/profiles/main.c",
        "compat/stdint.h",
    ):
        p = local_sdk / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")

    elf_paths = [
        "/home/ci/runner/work/FreeRTOS-v11/tasks.c",
        "/home/ci/runner/work/FreeRTOS-v11/portable/GCC/ARM_CM33/port.c",
        "/home/ci/runner/work/FreeRTOS-v11/gdr-fixture/profiles/main.c",
        "/opt/toolchain/arm-gnu/arm-none-eabi/include/stdint.h",
    ]
    local_files = collect_local_source_files([str(local_sdk)])
    mappings = infer_elf_prefix_mappings(elf_paths, local_files, [str(local_sdk)])
    assert mappings == (("/home/ci/runner/work/FreeRTOS-v11", str(local_sdk)),)

    # Windows build path -> macOS/POSIX local SDK
    win_elf_paths = [
        r"D:\jenkins\ws\rt-thread-repo\portable\GCC\ARM_CM33\port.c",
        r"D:\jenkins\ws\rt-thread-repo\gdr-fixture\profiles\main.c",
    ]
    win_mappings = infer_elf_prefix_mappings(
        win_elf_paths, local_files, [str(local_sdk)]
    )
    assert win_mappings == (("D:/jenkins/ws/rt-thread-repo", str(local_sdk)),)


def test_resolver_parent_expansion_and_plus_fallback(tmp_path):
    ws = tmp_path / "ws"
    sdk = ws / "sdk"
    ext = ws / "ext"
    build = sdk / "build"
    for folder in (sdk / "kernel", ext / "inc", build):
        folder.mkdir(parents=True, exist_ok=True)
    tasks = sdk / "kernel" / "tasks.c"
    hdr = ext / "inc" / "common.h"
    toolchain_hdr = ws / "toolchain" / "arm-none-eabi" / "include" / "stdint.h"
    toolchain_hdr.parent.mkdir(parents=True, exist_ok=True)
    tasks.write_text("", encoding="utf-8")
    hdr.write_text("", encoding="utf-8")
    toolchain_hdr.write_text("", encoding="utf-8")

    resolver = SourcePathResolver(
        local_roots=(str(sdk),),
        prefix_mappings=(("/remote/ci/project", str(sdk)),),
        search_dirs=(str(build), str(sdk)),
    )

    # 1. Remote path with internal ../ resolves cleanly to local SDK relative path
    assert (
        resolver.resolve_path("/remote/ci/project/examples/a/../../kernel/tasks.c")
        == "kernel/tasks.c"
    )
    # 2. Remote path escaping SDK into sibling external dir that exists locally
    assert resolver.resolve_path(
        "/remote/ci/project/build/../../ext/inc/common.h"
    ) == normalize_path(str(hdr))
    # 3. Relative ../ escaping SDK that exists locally via build search_dir
    assert resolver.resolve_path("../../ext/inc/common.h") == normalize_path(str(hdr))
    # 4. Local toolchain header with ../ simplifies to clean expanded local path
    local_tc_raw = str(
        ws
        / "toolchain"
        / "bin"
        / ".."
        / "lib"
        / ".."
        / "arm-none-eabi"
        / "include"
        / "stdint.h"
    )
    assert resolver.resolve_path(local_tc_raw) == normalize_path(str(toolchain_hdr))
    # 5. Remote path with ../ whose target does NOT exist locally -> + tail after last ../
    assert (
        resolver.resolve_path(
            "/remote/ci/arm-gcc/bin/../lib/gcc/15.2/../../../../arm-none-eabi/include/stdlib.h"
        )
        == "+arm-none-eabi/include/stdlib.h"
    )
    # 6. SDK-stripped relative path with ../ that fails local existence -> + tail
    assert (
        resolver.resolve_path("../../missing/vendor/hal.c") == "+missing/vendor/hal.c"
    )

    # 7. '+' prefix renders intact at wide width and can elide under right-compression
    table_wide = format_table(
        [["0", "0x1000", "fn", "+missing/vendor/hal.c:12", "cfi"]],
        ["#", "PC", "Function", "Source", "Method"],
        elastic={"Source": "right"},
        width=80,
    )
    assert "+missing/vendor/hal.c:12" in table_wide
    table_narrow = format_table(
        [["0", "0x1000", "fn", "+missing/vendor/hal.c:12", "cfi"]],
        ["#", "PC", "Function", "Source", "Method"],
        elastic={"Source": "right"},
        width=42,
    )
    assert "fn" in table_narrow
    assert "..or/hal.c:12" in table_narrow


def test_file_prefix_map_detection_skips_remote_root_inference(tmp_path):
    # A: Explicit compiler flag in .debug_str
    elf_flag = _build_synthetic_elf(
        {
            ".debug_str": b"GNU C11 15.2.1 -ffile-prefix-map=/ci/sdk=. -g\x00./src/main.c\x00",
        }
    )
    info_flag = scan_elf_source_info(elf_flag)
    assert info_flag.file_prefix_map is True
    assert "./src/main.c" in info_flag.paths

    # B: Structural DWARF .debug_line v4 paths (no flag in producer, as with real GCC)
    elf_dwarf = _build_synthetic_elf(
        {
            ".debug_str": b"GNU C11 15.2.1 -g\x00",
            ".debug_line": _build_debug_line_v4(["./inc", "./src"], [("main.c", 2)]),
        }
    )
    info_dwarf = scan_elf_source_info(elf_dwarf)
    assert info_dwarf.file_prefix_map is True
    assert "./src/main.c" in info_dwarf.paths

    # C: Non-prefix-mapped remote DWARF paths
    elf_remote = _build_synthetic_elf(
        {
            ".debug_str": b"GNU C11 15.2.1 -g\x00",
            ".debug_line": _build_debug_line_v4(
                ["/remote/sdk/src", "/remote/sdk/inc"], [("main.c", 1), ("cfg.h", 2)]
            ),
        }
    )
    info_remote = scan_elf_source_info(elf_remote)
    assert info_remote.file_prefix_map is False
    assert "/remote/sdk/src/main.c" in info_remote.paths

    # Resolver skips prefix_mappings when file_prefix_map=True and unwraps /./ seams
    resolver = SourcePathResolver(
        local_roots=(str(tmp_path),),
        prefix_mappings=(("/some/other", str(tmp_path)),),
        file_prefix_map=True,
    )
    assert resolver.prefix_mappings == ()
    assert (
        resolver.resolve_source_location(
            SourceLoc("./build/./src/main.c", 18, "./src/main.c")
        )
        == "src/main.c:18"
    )
    assert (
        resolver.resolve_source_location(
            SourceLoc("/remote/build/./src/main.c", 18, "./src/main.c")
        )
        == "src/main.c:18"
    )


def test_gdb_bridge_infers_remote_root_and_skips_when_prefix_mapped(
    monkeypatch, tmp_path
):
    ws = tmp_path / "sdk"
    (ws / "portable" / "CM33").mkdir(parents=True)
    (ws / "portable" / "CM33" / "port.c").write_text("", encoding="utf-8")

    # 1) Remote ELF (no -ffile-prefix-map): fuzzy inference shortens remote path
    remote_elf = tmp_path / "remote.elf"
    remote_elf.write_bytes(
        _build_synthetic_elf(
            {
                ".debug_str": b"GNU C11 -g\x00",
                ".debug_line": _build_debug_line_v4(
                    ["/builder/ws/FreeRTOS/portable/CM33"], [("port.c", 1)]
                ),
            }
        )
    )
    monkeypatch.setattr(bridge, "loaded_object_path", lambda: str(remote_elf))
    monkeypatch.setattr(bridge, "_current_cwd", lambda: str(ws))
    monkeypatch.setattr(bridge, "_STARTUP_CWD", str(ws))
    bridge.reset_loadable_ranges()

    assert (
        bridge.display_path("/builder/ws/FreeRTOS/portable/CM33/port.c")
        == "portable/CM33/port.c"
    )
    assert (
        bridge.display_path(
            "/builder/toolchain/bin/../../../arm-none-eabi/include/stdint.h"
        )
        == "+arm-none-eabi/include/stdint.h"
    )

    # 2) Same layout, but ELF compiled with -ffile-prefix-map=...=.: skip remote root inference
    mapped_elf = tmp_path / "mapped.elf"
    mapped_elf.write_bytes(
        _build_synthetic_elf(
            {
                ".debug_str": b"GNU C11 -ffile-prefix-map=/builder/ws/FreeRTOS=. -g\x00./portable/CM33/port.c\x00",
                ".debug_line": _build_debug_line_v4(
                    ["./portable/CM33"], [("port.c", 1)]
                ),
            }
        )
    )
    monkeypatch.setattr(bridge, "loaded_object_path", lambda: str(mapped_elf))
    bridge.reset_loadable_ranges()

    resolver = bridge.source_path_resolver()
    assert resolver.file_prefix_map is True
    assert resolver.prefix_mappings == ()
    assert (
        bridge.format_source_loc(
            SourceLoc("./build/./portable/CM33/port.c", 99, "./portable/CM33/port.c")
        )
        == "portable/CM33/port.c:99"
    )


@pytest.mark.skipif(
    shutil.which("arm-none-eabi-gcc") is None,
    reason="arm-none-eabi-gcc not installed",
)
def test_real_gcc_ffile_prefix_map_elf_detected(tmp_path):
    sdk = tmp_path / "sdk"
    sub = sdk / "sub"
    bdir = sdk / "build"
    sub.mkdir(parents=True)
    bdir.mkdir(parents=True)
    src = sub / "hello.c"
    src.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    obj = bdir / "hello.o"
    subprocess.run(
        [
            "arm-none-eabi-gcc",
            "-g",
            "-c",
            f"-ffile-prefix-map={sdk}=.",
            str(src),
            "-o",
            str(obj),
        ],
        cwd=str(bdir),
        check=True,
    )
    info = scan_elf_source_info(obj.read_bytes())
    assert info.file_prefix_map is True
    assert any(path.endswith("sub/hello.c") for path in info.paths)
