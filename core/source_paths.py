"""Pure ELF/DWARF source-path inference, local expansion, and shortening.

Resolves debug-info file paths against local SDK/workspace roots without any GDB
or target dependency:

1. Detects GCC/Clang ``-ffile-prefix-map=<dir>=.`` compilation (via explicit
   producer flags or DWARF ``.`` / ``./`` relative compilation units) and skips
   remote SDK-root inference when paths were already prefix-mapped at compile
   time.
2. When an ELF was compiled elsewhere, infers ``(elf_root, local_root)`` pairs
   by fuzzy suffix-consensus matching between ELF source paths and files inside
   local workspace/SDK roots.
3. For paths containing ``../`` traversal segments, attempts local filesystem
   expansion after SDK-root truncation; when local expansion still fails,
   truncates through the final ``../`` and prepends ``+``.
"""

from __future__ import annotations

import os
import re
import struct
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from core.elf import MAX_ELF_BYTES, iter_elf_section_headers
from core.formatting import SourceLoc

_DRIVE_RE = re.compile(r"^[A-Za-z]:(?:[\\/]|$)")
_PARENT_SEG_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_PREFIX_MAP_FLAG_RE = re.compile(
    r"(?:^|\s)-f(?:file|debug|macro)-prefix-map=[^\s=]+=\./?(?=\s|$)"
)
_SOURCE_EXTS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".c++",
        ".s",
        ".S",
        ".asm",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".inc",
        ".ipp",
        ".tcc",
    }
)
_PRUNED_WALK_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".tox"}
)
_MAX_ELF_PATHS = 8192
_MAX_LINE_UNITS = 4096
_MAX_LOCAL_WALK_ENTRIES = 100_000


@dataclass(frozen=True)
class ElfSourceInfo:
    """Source-path metadata recovered statically from an ELF's DWARF sections."""

    paths: tuple[str, ...] = ()
    file_prefix_map: bool = False


def is_abs_path(path: str) -> bool:
    """Return whether *path* is an absolute POSIX, UNC, or Windows drive path."""
    return bool(
        path.startswith(("/", "\\")) or (len(path) >= 3 and _DRIVE_RE.match(path))
    )


def has_parent_traversal(path: str) -> bool:
    """Return whether raw *path* contains any uncollapsed ``..`` segment."""
    return bool(_PARENT_SEG_RE.search(path))


def strip_after_last_parent(path: str) -> str | None:
    """Drop everything up to and including the last ``../``, prefixed with ``+``.

    Returns ``None`` when no segment follows the last ``..``.
    """
    unified = path.replace("\\", "/")
    raw_segs = unified.split("/")
    last_parent = -1
    for idx, seg in enumerate(raw_segs):
        if seg == "..":
            last_parent = idx
    if last_parent < 0:
        return None
    tail = [seg for seg in raw_segs[last_parent + 1 :] if seg and seg != "."]
    if not tail:
        return None
    return "+" + "/".join(tail)


def split_segments(path: str) -> tuple[str, tuple[str, ...]]:
    """Split *path* into ``(anchor, raw_segments)`` across ``/`` and ``\\``."""
    unified = path.replace("\\", "/")
    anchor = ""
    rest = unified
    if len(unified) >= 2 and unified[1] == ":" and unified[0].isalpha():
        if len(unified) >= 3 and unified[2] == "/":
            anchor = unified[:2].upper() + "/"
            rest = unified[3:]
        else:
            anchor = unified[:2].upper()
            rest = unified[2:]
    elif unified.startswith("//"):
        anchor = "/"
        rest = unified.lstrip("/")
    elif unified.startswith("/"):
        anchor = "/"
        rest = unified[1:]
    parts = tuple(seg for seg in rest.split("/") if seg and seg != ".")
    return anchor, parts


def normalize_path(path: str) -> str:
    """Lexically collapse ``.`` and ``..`` across POSIX and Windows paths."""
    if not path:
        return path
    anchor, parts = split_segments(path)
    stack: list[str] = []
    for seg in parts:
        if seg == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not anchor:
                stack.append("..")
        else:
            stack.append(seg)
    body = "/".join(stack)
    if anchor:
        return f"{anchor}{body}"
    return body or "."


def relative_under_root(path: str, root: str) -> str | None:
    """Return *path* relative to *root* using forward slashes, or ``None``."""
    p_anchor, p_segs = split_segments(normalize_path(path))
    r_anchor, r_segs = split_segments(normalize_path(root))
    if p_anchor and r_anchor and p_anchor == r_anchor:
        case_fold = p_anchor.endswith(":/")
        if len(p_segs) > len(r_segs) and _segs_equal(
            p_segs[: len(r_segs)], r_segs, case_fold=case_fold
        ):
            return "/".join(p_segs[len(r_segs) :])
    norm_path = os.path.normpath(path)
    norm_root = os.path.normpath(root)
    if "\\" not in path and "\\" not in root:
        try:
            real_path = os.path.realpath(norm_path)
            real_root = os.path.realpath(norm_root)
            rel = os.path.relpath(real_path, real_root)
        except (OSError, ValueError):
            return None
        if rel != "." and not rel.startswith("..") and not is_abs_path(rel):
            return rel.replace("\\", "/")
    return None


def _segs_equal(
    left: Sequence[str], right: Sequence[str], *, case_fold: bool = False
) -> bool:
    if len(left) != len(right):
        return False
    if case_fold:
        return all(a.lower() == b.lower() for a, b in zip(left, right, strict=True))
    return tuple(left) == tuple(right)


def _decode_cstr_at(data: bytes, offset: int) -> tuple[str | None, int]:
    if offset < 0 or offset >= len(data):
        return None, len(data)
    end = data.find(b"\x00", offset)
    if end < 0:
        return None, len(data)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _read_uleb128(data: bytes, offset: int) -> tuple[int | None, int]:
    result = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift > 63:
            return None, len(data)
    return None, len(data)


def _is_prefix_mapped_rel(text: str) -> bool:
    if text == ".":
        return False
    return text.startswith(("./", ".\\")) and not text.startswith(("./..", ".\\.."))


def _looks_like_source_path(text: str) -> bool:
    if not text or len(text) > 512:
        return False
    if any(ch in text for ch in ("\n", "\r", "\t")):
        return False
    if text.startswith("__") or "#define" in text:
        return False
    if "GNU C" in text and (" -m" in text or " -g" in text or " -O" in text):
        return False
    suffix = Path(text.replace("\\", "/")).suffix
    has_sep = "/" in text or "\\" in text
    return suffix in _SOURCE_EXTS and (
        has_sep or is_abs_path(text) or text.startswith("./")
    )


def _scan_debug_line_v2_v4(blob: bytes, endian: str) -> tuple[list[str], bool]:
    """Extract directory/file combinations from DWARF v2-v4 ``.debug_line`` units."""
    endian_ch = "<" if endian == "little" else ">"
    paths: list[str] = []
    has_dot_dir = False
    offset = 0
    units = 0
    while offset + 10 <= len(blob) and units < _MAX_LINE_UNITS:
        units += 1
        (unit_len,) = struct.unpack_from(f"{endian_ch}I", blob, offset)
        if unit_len in {0, 0xFFFFFFFF}:
            break
        unit_end = offset + 4 + unit_len
        if unit_end > len(blob) or offset + 12 > unit_end:
            break
        (version,) = struct.unpack_from(f"{endian_ch}H", blob, offset + 4)
        cursor = offset + 6
        if version not in (2, 3, 4):
            offset = unit_end
            continue
        if cursor + 4 > unit_end:
            offset = unit_end
            continue
        cursor += 4  # header_length
        # min_insn_len, [max_ops_per_insn if v4], default_is_stmt, line_base, line_range, opcode_base
        fixed_bytes = 6 if version >= 4 else 5
        if cursor + fixed_bytes > unit_end:
            offset = unit_end
            continue
        opcode_base = blob[cursor + fixed_bytes - 1]
        cursor += fixed_bytes + max(0, opcode_base - 1)
        if cursor >= unit_end:
            offset = unit_end
            continue
        include_dirs: list[str] = []
        while cursor < unit_end:
            if blob[cursor] == 0:
                cursor += 1
                break
            entry, cursor = _decode_cstr_at(blob, cursor)
            if entry is None:
                break
            include_dirs.append(entry)
            if entry == "." or _is_prefix_mapped_rel(entry):
                has_dot_dir = True
        while cursor < unit_end:
            if blob[cursor] == 0:
                break
            fname, cursor = _decode_cstr_at(blob, cursor)
            if fname is None:
                break
            dir_idx, cursor = _read_uleb128(blob, cursor)
            _, cursor = _read_uleb128(blob, cursor)
            _, cursor = _read_uleb128(blob, cursor)
            if dir_idx is None:
                break
            if dir_idx == 0:
                full = fname
            elif 1 <= dir_idx <= len(include_dirs):
                base_dir = include_dirs[dir_idx - 1].rstrip("/\\")
                full = f"{base_dir}/{fname}" if base_dir else fname
            else:
                full = fname
            if full:
                paths.append(full)
                if _is_prefix_mapped_rel(full) or "/./" in full.replace("\\", "/"):
                    has_dot_dir = True
            if len(paths) >= _MAX_ELF_PATHS:
                return paths, has_dot_dir
        offset = unit_end
    return paths, has_dot_dir


def scan_elf_source_info(data: bytes) -> ElfSourceInfo:
    """Scan ELF DWARF string/line sections for source paths and prefix-map hints."""
    if len(data) < 0x40 or len(data) > MAX_ELF_BYTES or data[:4] != b"\x7fELF":
        return ElfSourceInfo()
    endian = "big" if data[5] == 2 else "little"
    table = iter_elf_section_headers(data)
    if table is None:
        return ElfSourceInfo()
    wanted = {".debug_str", ".debug_line_str", ".debug_line"}
    payloads: dict[str, bytes] = {}
    for header in table.headers:
        sec_name, _ = _decode_cstr_at(table.names, header.name_offset)
        if sec_name in wanted and header.offset and header.size:
            payloads[sec_name] = data[header.offset : header.offset + header.size]
    if not payloads:
        return ElfSourceInfo()

    found_paths: list[str] = []
    seen_paths: set[str] = set()
    file_prefix_map = False

    def _add(path: str) -> None:
        if len(found_paths) >= _MAX_ELF_PATHS or path in seen_paths:
            return
        seen_paths.add(path)
        found_paths.append(path)

    for sec_name in (".debug_str", ".debug_line_str"):
        blob = payloads.get(sec_name)
        if not blob:
            continue
        for chunk in blob.split(b"\x00"):
            if not chunk:
                continue
            text = chunk.decode("utf-8", errors="replace")
            if _PREFIX_MAP_FLAG_RE.search(text):
                file_prefix_map = True
            if _looks_like_source_path(text):
                _add(text)
                if _is_prefix_mapped_rel(text) or "/./" in text.replace("\\", "/"):
                    file_prefix_map = True

    line_blob = payloads.get(".debug_line")
    if line_blob:
        line_paths, has_dot_dir = _scan_debug_line_v2_v4(line_blob, endian)
        if has_dot_dir:
            file_prefix_map = True
        for lp in line_paths:
            _add(lp)

    return ElfSourceInfo(paths=tuple(found_paths), file_prefix_map=file_prefix_map)


def inspect_elf_sources(path: str | Path) -> ElfSourceInfo:
    """Read an ELF from *path* and extract its DWARF source-path metadata."""
    try:
        file_obj = Path(path)
        if file_obj.stat().st_size > MAX_ELF_BYTES:
            return ElfSourceInfo()
        return scan_elf_source_info(file_obj.read_bytes())
    except OSError:
        return ElfSourceInfo()


def collect_local_source_files(
    roots: Sequence[str],
    *,
    target_basenames: frozenset[str] | None = None,
    max_entries: int = _MAX_LOCAL_WALK_ENTRIES,
) -> list[str]:
    """Collect local source/header paths under *roots*, pruned and bounded."""
    lower_targets = (
        frozenset(name.lower() for name in target_basenames)
        if target_basenames is not None
        else None
    )
    results: list[str] = []
    seen_real: set[str] = set()
    visited = 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, topdown=True):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in _PRUNED_WALK_DIRS and not d.startswith(".")
            )
            for fname in filenames:
                visited += 1
                if visited > max_entries:
                    return results
                if lower_targets is not None and fname.lower() not in lower_targets:
                    continue
                if lower_targets is None and Path(fname).suffix not in _SOURCE_EXTS:
                    continue
                full = os.path.join(dirpath, fname)
                real = os.path.realpath(full)
                if real not in seen_real:
                    seen_real.add(real)
                    results.append(os.path.normpath(full))
    return results


def _shared_suffix_len(
    a_segs: tuple[str, ...], b_segs: tuple[str, ...], *, case_fold: bool
) -> int:
    count = 0
    limit = min(len(a_segs), len(b_segs))
    while count < limit:
        a = a_segs[len(a_segs) - 1 - count]
        b = b_segs[len(b_segs) - 1 - count]
        if a != b and (not case_fold or a.lower() != b.lower()):
            break
        count += 1
    return count


def _dir_token_overlap(a_prefix: tuple[str, ...], b_prefix: tuple[str, ...]) -> int:
    a_set = {seg.lower() for seg in a_prefix if seg}
    b_set = {seg.lower() for seg in b_prefix if seg}
    return len(a_set & b_set)


def _join_anchor_segments(anchor: str, segs: tuple[str, ...]) -> str:
    body = "/".join(segs)
    if anchor:
        return f"{anchor}{body}" if body else anchor.rstrip("/") or "/"
    return body or "."


def infer_elf_prefix_mappings(
    elf_paths: Sequence[str],
    local_files: Sequence[str],
    local_roots: Sequence[str] = (),
) -> tuple[tuple[str, str], ...]:
    """Infer ordered ``(elf_root, local_root)`` pairs via suffix consensus."""
    if not elf_paths or not local_files:
        return ()

    norm_local_roots = {
        normalize_path(root).rstrip("/").lower() for root in local_roots if root
    }
    by_name: dict[str, list[tuple[str, tuple[str, ...]]]] = defaultdict(list)
    for local_file in local_files:
        l_norm = normalize_path(local_file)
        l_anchor, l_segs = split_segments(l_norm)
        if l_segs:
            by_name[l_segs[-1].lower()].append((l_anchor, l_segs))

    # Votes keyed by (elf_anchor, elf_prefix_segs, local_anchor, local_prefix_segs)
    votes: dict[
        tuple[str, tuple[str, ...], str, tuple[str, ...]],
        list[int],  # [score, file_count, max_depth]
    ] = {}
    seen_elf: set[str] = set()

    for raw_elf in elf_paths:
        e_norm = normalize_path(raw_elf)
        if not is_abs_path(e_norm) or e_norm in seen_elf:
            continue
        seen_elf.add(e_norm)
        e_anchor, e_segs = split_segments(e_norm)
        if len(e_segs) < 2:
            continue
        candidates = by_name.get(e_segs[-1].lower())
        if not candidates:
            continue
        fold_case = e_anchor.endswith(":/")
        best_rank: tuple[int, int, int, int] = (-1, -1, -1, -1)
        best_pairs: list[tuple[tuple[str, ...], str, tuple[str, ...], int]] = []
        for l_anchor, l_segs in candidates:
            if fold_case or l_anchor.endswith(":/"):
                depth = _shared_suffix_len(e_segs, l_segs, case_fold=True)
            else:
                depth = _shared_suffix_len(e_segs, l_segs, case_fold=False)
                if depth == 0:
                    depth = _shared_suffix_len(e_segs, l_segs, case_fold=True)
            if depth == 0 or depth >= len(e_segs) or depth >= len(l_segs):
                continue
            e_pref = e_segs[:-depth]
            l_pref = l_segs[:-depth]
            exact_case = int(
                _segs_equal(e_segs[-depth:], l_segs[-depth:], case_fold=False)
            )
            l_pref_str = _join_anchor_segments(l_anchor, l_pref).rstrip("/").lower()
            is_known_root = int(l_pref_str in norm_local_roots)
            dir_overlap = _dir_token_overlap(e_pref, l_pref)
            rank = (depth, exact_case, is_known_root, dir_overlap)
            if rank > best_rank:
                best_rank = rank
                best_pairs = [(e_pref, l_anchor, l_pref, depth)]
            elif rank == best_rank:
                best_pairs.append((e_pref, l_anchor, l_pref, depth))
        for e_pref, l_anchor, l_pref, depth in best_pairs:
            l_pref_str = _join_anchor_segments(l_anchor, l_pref).rstrip("/").lower()
            weight = depth * depth + (2 if l_pref_str in norm_local_roots else 0)
            key = (e_anchor, e_pref, l_anchor, l_pref)
            acc = votes.get(key)
            if acc is None:
                votes[key] = [weight, 1, depth]
            else:
                acc[0] += weight
                acc[1] += 1
                if depth > acc[2]:
                    acc[2] = depth

    if not votes:
        return ()

    # Consolidate child votes that strictly sit inside a parent vote at equal offsets.
    vote_keys = list(votes)
    for child in vote_keys:
        c_ea, c_ep, c_la, c_lp = child
        c_case = c_ea.endswith(":/") or c_la.endswith(":/")
        for parent in vote_keys:
            if parent == child:
                continue
            p_ea, p_ep, p_la, p_lp = parent
            if p_ea != c_ea or p_la != c_la:
                continue
            if len(p_ep) >= len(c_ep) or len(p_lp) >= len(c_lp):
                continue
            e_delta = c_ep[len(p_ep) :]
            l_delta = c_lp[len(p_lp) :]
            if (
                _segs_equal(c_ep[: len(p_ep)], p_ep, case_fold=c_case)
                and _segs_equal(c_lp[: len(p_lp)], p_lp, case_fold=c_case)
                and _segs_equal(e_delta, l_delta, case_fold=c_case)
            ):
                p_acc = votes[parent]
                c_acc = votes[child]
                p_acc[0] += c_acc[0]
                p_acc[1] += c_acc[1]
                p_acc[2] = max(p_acc[2], c_acc[2] + len(e_delta))

    accepted: list[tuple[int, int, int, str, str]] = []
    for (e_anchor, e_pref, l_anchor, l_pref), (
        score,
        count,
        max_depth,
    ) in votes.items():
        local_str = _join_anchor_segments(l_anchor, l_pref)
        is_known = local_str.rstrip("/").lower() in norm_local_roots
        root_seg_match = (
            bool(e_pref) and bool(l_pref) and e_pref[-1].lower() == l_pref[-1].lower()
        )
        if (
            max_depth >= 2
            or count >= 2
            or (is_known and (count >= 1 or root_seg_match))
        ):
            elf_str = _join_anchor_segments(e_anchor, e_pref)
            accepted.append((score, count, -len(e_pref), elf_str, local_str))

    accepted.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    result: list[tuple[str, str]] = []
    seen_elf_roots: set[str] = set()
    for _score, _count, _neg_len, elf_str, local_str in accepted:
        key = elf_str.rstrip("/").lower()
        if key not in seen_elf_roots:
            seen_elf_roots.add(key)
            result.append((elf_str, local_str))
    # Sort longer remote sub-prefixes ahead of ancestors if distinct non-merged pairs exist.
    return tuple(result)


def _strip_prefix_map_seam(path: str) -> str:
    """Unwrap ``<comp_dir>/./<subpath>`` emitted by GDB with ``-ffile-prefix-map``."""
    unified = path.replace("\\", "/")
    idx = unified.find("/./")
    if idx < 0:
        return path
    tail = unified[idx + 3 :]
    if tail and not tail.startswith(".."):
        return tail
    return path


class SourcePathResolver:
    """Shorten debug-info file paths against local roots, remote SDK inference,
    and bounded ``../`` local expansion.
    """

    def __init__(
        self,
        local_roots: Sequence[str] = (),
        prefix_mappings: Sequence[tuple[str, str]] = (),
        *,
        search_dirs: Sequence[str] = (),
        file_prefix_map: bool = False,
        exists_fn: Callable[[str], bool] = os.path.exists,
    ) -> None:
        self.local_roots = tuple(root for root in local_roots if root)
        # Longest elf_prefix first for accurate matching if submodules overlap.
        self.prefix_mappings = (
            ()
            if file_prefix_map
            else tuple(
                sorted(
                    prefix_mappings,
                    key=lambda pair: len(split_segments(pair[0])[1]),
                    reverse=True,
                )
            )
        )
        dirs: list[str] = []
        seen: set[str] = set()
        for cand in (
            *search_dirs,
            *(local for _elf, local in self.prefix_mappings),
            *self.local_roots,
        ):
            if not cand:
                continue
            norm_c = normalize_path(cand)
            if norm_c not in seen:
                seen.add(norm_c)
                dirs.append(norm_c)
        self.search_dirs = tuple(dirs)
        self.file_prefix_map = file_prefix_map
        self.exists_fn = exists_fn

    def _shorten_under_local_roots(self, path: str) -> str | None:
        for root in self.local_roots:
            rel = relative_under_root(path, root)
            if rel is not None:
                return rel
        return None

    def _match_prefix_mapping(self, norm: str) -> tuple[str, str] | None:
        """Return ``(relative_tail, mapped_local_root)`` if *norm* falls under an ELF root."""
        for elf_root, local_root in self.prefix_mappings:
            rel = relative_under_root(norm, elf_root)
            if rel is not None:
                return rel, local_root
        return None

    def _expand_locally(
        self,
        raw_path: str,
        norm: str,
        *,
        mapped_local_root: str | None = None,
        mapped_rel: str | None = None,
    ) -> str | None:
        candidates: list[str] = []
        if mapped_local_root is not None:
            # Also replay raw ../ traversal from the mapped root if raw had ..
            for elf_root, local_root in self.prefix_mappings:
                if local_root == mapped_local_root:
                    raw_unified = raw_path.replace("\\", "/")
                    raw_tail = _tail_after_prefix(raw_unified, elf_root)
                    if raw_tail is not None:
                        candidates.append(normalize_path(f"{local_root}/{raw_tail}"))
            if mapped_rel is not None:
                candidates.append(normalize_path(f"{mapped_local_root}/{mapped_rel}"))
        if is_abs_path(norm):
            candidates.append(norm)
        elif not is_abs_path(raw_path):
            raw_clean = raw_path.replace("\\", "/").removeprefix("./")
            for base in self.search_dirs:
                candidates.append(normalize_path(f"{base}/{raw_clean}"))
                if norm != raw_clean and not norm.startswith(".."):
                    candidates.append(normalize_path(f"{base}/{norm}"))
        else:
            candidates.append(norm)

        # If raw starts inside a remote prefix even though norm escaped it via ../
        if is_abs_path(raw_path) and has_parent_traversal(raw_path):
            for elf_root, local_root in self.prefix_mappings:
                raw_tail = _tail_after_prefix(raw_path.replace("\\", "/"), elf_root)
                if raw_tail is not None:
                    candidates.append(normalize_path(f"{local_root}/{raw_tail}"))

        seen: set[str] = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            if self.exists_fn(cand):
                return cand
        return None

    def resolve_path(self, raw_path: str) -> str:
        """Resolve and shorten *raw_path* according to configured roots and mode."""
        if not raw_path:
            return raw_path
        had_parent = has_parent_traversal(raw_path)
        seam_path = _strip_prefix_map_seam(raw_path)
        if seam_path != raw_path and (
            self.file_prefix_map
            or not is_abs_path(raw_path)
            or self.exists_fn(
                normalize_path(f"{self.local_roots[0]}/{seam_path}")
                if self.local_roots
                else seam_path
            )
        ):
            return self.resolve_path(seam_path)

        norm = normalize_path(raw_path)

        # Step 1: SDK root truncation (local workspace roots, or fuzzy ELF root).
        sdk_rel: str | None = None
        mapped_local_root: str | None = None
        if is_abs_path(norm):
            sdk_rel = self._shorten_under_local_roots(norm)
            if sdk_rel is None and not self.file_prefix_map:
                matched = self._match_prefix_mapping(norm)
                if matched is not None:
                    sdk_rel, mapped_local_root = matched
        else:
            norm = norm.removeprefix("./")
            if not norm.startswith(".."):
                sdk_rel = norm

        # Step 2: Try local expansion regardless of whether Step 1 succeeded.
        should_expand = (
            had_parent
            or mapped_local_root is not None
            or (not is_abs_path(norm) and bool(self.search_dirs))
        )
        if should_expand:
            expanded = self._expand_locally(
                raw_path,
                norm,
                mapped_local_root=mapped_local_root,
                mapped_rel=sdk_rel,
            )
            if expanded is not None:
                rel = self._shorten_under_local_roots(expanded)
                return rel if rel is not None else expanded
            if had_parent:
                plus_tail = strip_after_last_parent(raw_path)
                if plus_tail is not None:
                    return plus_tail

        if sdk_rel is not None:
            return sdk_rel
        return norm

    def resolve_source_location(self, source: SourceLoc | None) -> str:
        """Render *source* as a shortened ``path:line`` display string."""
        if source is None:
            return ""
        fullname = source.fullname
        name = source.name
        if (
            fullname
            and name
            and _is_prefix_mapped_rel(name)
            and ("/./" in fullname.replace("\\", "/") or self.file_prefix_map)
        ):
            display = self.resolve_path(name)
        else:
            primary = fullname or name
            display = self.resolve_path(primary) if primary else ""
            if (
                is_abs_path(display)
                and not self.exists_fn(display)
                and name
                and name != primary
                and (not is_abs_path(name) or has_parent_traversal(name))
            ):
                alt = self.resolve_path(name)
                if not is_abs_path(alt) or self.exists_fn(alt):
                    display = alt
        return f"{display}:{source.line}" if display else str(source.line)


def relative_path(path: str, roots: Sequence[str] = ()) -> str:
    """Shorten a file path relative to the first containing root."""
    return SourcePathResolver(local_roots=roots).resolve_path(path)


def format_source_location(source: SourceLoc | None, roots: Sequence[str] = ()) -> str:
    """Render one :class:`SourceLoc` as a concise ``path:line`` cell."""
    return SourcePathResolver(local_roots=roots).resolve_source_location(source)


def _tail_after_prefix(raw_unified: str, prefix: str) -> str | None:
    """Return raw suffix after *prefix* when *raw_unified* starts with it."""
    p_norm = normalize_path(prefix).rstrip("/")
    r_clean = raw_unified.rstrip("/")
    fold = len(p_norm) >= 2 and p_norm[1] == ":"
    lhs = r_clean.lower() if fold else r_clean
    rhs = p_norm.lower() if fold else p_norm
    if lhs == rhs:
        return ""
    sep_rhs = rhs + "/"
    if lhs.startswith(sep_rhs):
        return r_clean[len(sep_rhs) :]
    return None
