#!/usr/bin/env python3
"""Run repository integration tooling from any working directory."""

import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ci.testing.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
