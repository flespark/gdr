"""Repository-owned paths for integration inputs and generated artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "ci" / ".cache"
DIRECTORIES = (
    "tools",
    "sources",
    "build",
    "fixtures",
    "env",
    "logs",
    "locks",
    "downloads",
    "containers",
    "tmp",
)


def initialize(destination: Path | None = None, *, prompt: bool = False) -> Path:
    """Create the cache or link an absent cache to a selected directory."""
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    if destination is not None:
        destination = destination.expanduser().absolute()
        resolved = destination.resolve()
        if (
            resolved == CACHE.absolute()
            or CACHE.absolute() in resolved.parents
            or resolved == ROOT
            or resolved in ROOT.parents
        ):
            raise ValueError(
                "cache destination must not contain the repository or recurse into ci/.cache"
            )
        if CACHE.exists() or CACHE.is_symlink():
            if CACHE.resolve() != destination.resolve():
                raise ValueError("ci/.cache already exists; move it explicitly first")
        else:
            destination.mkdir(parents=True, exist_ok=True)
            CACHE.symlink_to(destination, target_is_directory=True)
    elif (
        not CACHE.exists() and not CACHE.is_symlink() and prompt and sys.stdin.isatty()
    ):
        answer = input(
            "CI cache directory [Enter: ci/.cache; or external directory]: "
        ).strip()
        if answer:
            return initialize(Path(answer))
    if CACHE.is_symlink() and not CACHE.exists():
        raise ValueError("ci/.cache is a broken link; restore its destination first")
    for name in DIRECTORIES:
        (CACHE / name).mkdir(parents=True, exist_ok=True)
    return CACHE


def host_id() -> str:
    """Return a platform key for tools and Python environments."""
    import platform

    return f"{sys.platform}-{platform.machine()}"
