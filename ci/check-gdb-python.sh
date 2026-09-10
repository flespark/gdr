#!/usr/bin/env bash
# Verify the GDB used by a closed-loop job: the embedded Python interpreter
# and, when the lane passes its target architecture as $1 (e.g. riscv:rv64),
# that the GDB actually supports it.
set -euo pipefail

GDB_BIN="${GDR_GDB:-gdb-multiarch}"
MIN_PYTHON="${GDR_MIN_EMBEDDED_PYTHON:-3.10}"
EXPECTED_GDB_MAJOR="${GDR_EXPECTED_GDB_MAJOR:-}"
EXPECTED_PYTHON="${GDR_EXPECTED_EMBEDDED_PYTHON:-}"

if ! command -v "$GDB_BIN" >/dev/null; then
    echo "[gdr-ci] FAILED: GDB executable not found: $GDB_BIN" >&2
    exit 1
fi

gdb_version="$(LC_ALL=C "$GDB_BIN" --version | head -n 1)"
echo "[gdr-ci] $gdb_version"
if [[ -n "$EXPECTED_GDB_MAJOR" ]] &&
    ! grep -Eq " ${EXPECTED_GDB_MAJOR}(\\.| )" <<<"$gdb_version"; then
    echo "[gdr-ci] FAILED: expected GDB major version $EXPECTED_GDB_MAJOR" >&2
    exit 1
fi

# Reason: GDB's version and the host `python3` do not identify the linked
# CPython ABI. Query the interpreter that will actually import GDR instead.
# Do the comparison in this shell: an exception raised by `python` in GDB's
# batch mode can still leave GDB with a successful process status.
embedded_version="$(LC_ALL=C "$GDB_BIN" --nx --quiet --batch \
    --ex 'python import sys; print(".".join(map(str, sys.version_info[:3])))')"
if [[ ! "$embedded_version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
    echo "[gdr-ci] FAILED: could not read embedded Python version: $embedded_version" >&2
    exit 1
fi

IFS=. read -r embedded_major embedded_minor _ <<<"$embedded_version"
IFS=. read -r minimum_major minimum_minor <<<"$MIN_PYTHON"
if ((embedded_major < minimum_major || (\
    embedded_major == minimum_major && embedded_minor < minimum_minor))); then
    echo "[gdr-ci] FAILED: GDR requires embedded Python $MIN_PYTHON+; found $embedded_version" >&2
    exit 1
fi
if [[ -n "$EXPECTED_PYTHON" && "$embedded_major.$embedded_minor" != "$EXPECTED_PYTHON" ]]; then
    echo "[gdr-ci] FAILED: expected embedded Python $EXPECTED_PYTHON; found $embedded_version" >&2
    exit 1
fi
echo "[gdr-ci] embedded Python: $embedded_version"

# Reason: a GDB can exist and embed Python yet lack the lane's target
# architecture (an ARM-only build on the RISC-V lane); that used to surface
# as every closed-loop test failing on a half-initialised session instead of
# a clear pre-flight error. Match the success message rather than the exit
# status, like the Python probe above: batch-mode command errors can still
# leave some GDB builds with a successful process status.
REQUIRED_ARCH="${1:-}"
if [[ -n "$REQUIRED_ARCH" ]]; then
    arch_output="$(LC_ALL=C "$GDB_BIN" --nx --quiet --batch \
        --ex "set architecture $REQUIRED_ARCH" 2>&1 || true)"
    if [[ "$arch_output" != *"architecture is set to"* ]]; then
        echo "[gdr-ci] FAILED: $GDB_BIN lacks the '$REQUIRED_ARCH' architecture this lane requires" >&2
        echo "[gdr-ci] GDB said: $arch_output" >&2
        exit 1
    fi
    echo "[gdr-ci] architecture support: $REQUIRED_ARCH"
fi
