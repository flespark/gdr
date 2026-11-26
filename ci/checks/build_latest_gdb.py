#!/usr/bin/env python3
"""Fetch and build the latest stable upstream GNU GDB with Python support.

Queries GNU release mirrors for the highest numeric stable ``gdb-X.Y(.Z).tar.xz``
tarball (ignoring release candidates and ``.90+`` prereleases), compiles a
multi-architecture GDB linked against the host ``python3`` interpreter and
``expat`` XML support, and installs a ``gdb-multiarch`` symlink under
``<prefix>/bin``.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MIRRORS: tuple[str, ...] = (
    "https://mirrors.tuna.tsinghua.edu.cn/gnu/gdb/",
    "https://mirrors.ustc.edu.cn/gnu/gdb/",
    "https://ftpmirror.gnu.org/gnu/gdb/",
    "https://ftp.gnu.org/gnu/gdb/",
)

_TARBALL_RE = re.compile(r'href=["\']?(gdb-(\d+)\.(\d+)(?:\.(\d+))?\.tar\.xz)["\']?')


def select_latest_stable_tarball(index_html: str) -> tuple[str, tuple[int, int, int]]:
    """Return ``(filename, (major, minor, patch))`` for the newest stable GDB."""
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for match in _TARBALL_RE.finditer(index_html):
        filename = match.group(1)
        major = int(match.group(2))
        minor = int(match.group(3))
        patch = int(match.group(4)) if match.group(4) is not None else 0
        # NOTE: GNU GDB uses .90+ prerelease snapshots (e.g. 16.3.90); exclude
        # them so only official stable releases are selected.
        if minor >= 90 or patch >= 90:
            continue
        candidates.append(((major, minor, patch), filename))
    if not candidates:
        raise RuntimeError("no stable gdb-X.Y(.Z).tar.xz tarball found in mirror index")
    version, filename = max(candidates, key=lambda item: item[0])
    return filename, version


def _http_get_once(
    url: str, *, timeout: float
) -> tuple[bytes | None, Exception | None]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "gdr-ci-gdb-builder/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, exc


def _http_get_bytes(url: str, *, timeout: float = 30.0, retries: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        data, err = _http_get_once(url, timeout=timeout)
        if data is not None:
            return data
        last_error = err
        if attempt < retries:
            time.sleep(min(2 * attempt, 6))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def _download_once(url: str, destination: Path, *, timeout: float) -> Exception | None:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "gdr-ci-gdb-builder/1.0"}
        )
        with (
            urllib.request.urlopen(req, timeout=timeout) as resp,
            destination.open("wb") as out,
        ):
            shutil.copyfileobj(resp, out)
        if destination.stat().st_size > 0:
            return None
        return RuntimeError(f"empty response body for {url}")
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as exc:
        return exc


def _download_file(
    url: str, destination: Path, *, timeout: float = 60.0, retries: int = 3
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        err = _download_once(url, destination, timeout=timeout)
        if err is None:
            return
        last_error = err
        destination.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(min(2 * attempt, 6))
    raise RuntimeError(f"failed to download {url}: {last_error}")


def resolve_and_download_latest(
    mirrors: tuple[str, ...], destination_dir: Path
) -> tuple[Path, tuple[int, int, int]]:
    """Query *mirrors* in order, select the latest stable tarball, and download it."""
    errors: list[str] = []
    for base in mirrors:
        mirror = base.rstrip("/") + "/"
        try:
            html = _http_get_bytes(mirror).decode("utf-8", errors="replace")
            filename, version = select_latest_stable_tarball(html)
            tarball = destination_dir / filename
            print(
                f"[gdr-ci] selected latest stable GDB {'.'.join(map(str, version))} "
                f"from {mirror}{filename}",
                flush=True,
            )
            _download_file(f"{mirror}{filename}", tarball)
            return tarball, version
        except RuntimeError as exc:
            errors.append(f"{mirror}: {exc}")
    raise RuntimeError(
        "could not resolve/download latest stable GDB from any mirror:\n"
        + "\n".join(errors)
    )


def build_configure_command(
    source_dir: Path, prefix: Path, python_bin: str
) -> list[str]:
    """Assemble ``configure`` flags for multi-ISA Python-enabled GDB."""
    return [
        str(source_dir / "configure"),
        f"--prefix={prefix}",
        "--enable-targets=all",
        f"--with-python={python_bin}",
        "--with-expat",
        "--disable-binutils",
        "--disable-ld",
        "--disable-gold",
        "--disable-gas",
        "--disable-sim",
        "--disable-gprof",
        "--disable-gprofng",
        "--disable-nls",
        "--disable-werror",
        "MAKEINFO=true",
    ]


def build_latest_gdb(
    prefix: Path,
    *,
    mirrors: tuple[str, ...] = DEFAULT_MIRRORS,
    python_bin: str = sys.executable,
    jobs: int | None = None,
) -> Path:
    """Download, compile, install, and smoke-verify the latest stable GNU GDB."""
    parallel = jobs or max(os.cpu_count() or 1, 1)
    with tempfile.TemporaryDirectory(prefix="gdr-build-gdb-") as tmpdir:
        tmp = Path(tmpdir)
        tarball, version = resolve_and_download_latest(mirrors, tmp)
        src_root = tmp / "src"
        build_dir = tmp / "build"
        src_root.mkdir()
        build_dir.mkdir()

        subprocess.run(
            ["tar", "-xf", str(tarball), "-C", str(src_root)],
            check=True,
        )
        extracted = [entry for entry in src_root.iterdir() if entry.is_dir()]
        if len(extracted) != 1:
            raise RuntimeError(
                f"expected one extracted source directory in {src_root}, found {extracted}"
            )
        source_dir = extracted[0]

        configure_cmd = build_configure_command(source_dir, prefix, python_bin)
        print(
            f"[gdr-ci] configuring GDB {'.'.join(map(str, version))} "
            f"(jobs={parallel}, prefix={prefix})",
            flush=True,
        )
        subprocess.run(configure_cmd, cwd=build_dir, check=True)
        subprocess.run(["make", f"-j{parallel}", "all-gdb"], cwd=build_dir, check=True)
        subprocess.run(["make", "install-gdb"], cwd=build_dir, check=True)

    gdb_bin = prefix / "bin" / "gdb"
    multiarch_bin = prefix / "bin" / "gdb-multiarch"
    if not gdb_bin.exists():
        raise RuntimeError(f"built GDB binary missing at {gdb_bin}")
    if multiarch_bin.exists() or multiarch_bin.is_symlink():
        multiarch_bin.unlink()
    multiarch_bin.symlink_to(gdb_bin.name)

    subprocess.run([str(multiarch_bin), "--version"], check=True)
    subprocess.run(
        [
            str(multiarch_bin),
            "--nx",
            "--quiet",
            "--batch",
            "--ex",
            "python import sys; assert sys.version_info[:2] >= (3, 10), sys.version",
            "--ex",
            "set architecture armv7",
        ],
        check=True,
    )
    print(f"[gdr-ci] installed latest stable GDB at {multiarch_bin}", flush=True)
    return multiarch_bin


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Build and install the latest stable upstream GNU GDB."
    )
    parser.add_argument(
        "--prefix",
        type=Path,
        default=Path("/opt/gdb-latest"),
        help="Installation prefix (default: /opt/gdb-latest)",
    )
    parser.add_argument(
        "--python",
        default=shutil.which("python3") or sys.executable,
        help="Python 3 executable to embed in GDB",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Parallel make job count (default: CPU count)",
    )
    parser.add_argument(
        "--mirror",
        action="append",
        dest="mirrors",
        help="Override GNU GDB mirror base URL (repeatable)",
    )
    args = parser.parse_args(argv)
    mirrors = tuple(args.mirrors) if args.mirrors else DEFAULT_MIRRORS
    build_latest_gdb(
        args.prefix,
        mirrors=mirrors,
        python_bin=args.python,
        jobs=args.jobs,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
