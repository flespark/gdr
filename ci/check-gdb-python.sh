#!/usr/bin/env bash
# Verify the Python interpreter embedded in the GDB used by a closed-loop job.
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
if [[ -n "$EXPECTED_GDB_MAJOR" ]] && \
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
if ((embedded_major < minimum_major || \
    (embedded_major == minimum_major && embedded_minor < minimum_minor))); then
    echo "[gdr-ci] FAILED: GDR requires embedded Python $MIN_PYTHON+; found $embedded_version" >&2
    exit 1
fi
if [[ -n "$EXPECTED_PYTHON" && "$embedded_major.$embedded_minor" != "$EXPECTED_PYTHON" ]]; then
    echo "[gdr-ci] FAILED: expected embedded Python $EXPECTED_PYTHON; found $embedded_version" >&2
    exit 1
fi
echo "[gdr-ci] embedded Python: $embedded_version"
