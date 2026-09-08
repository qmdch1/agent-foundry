"""Portable final generation/edit token counter. No prompts or per-edit history in Git."""

import re
import tempfile
from pathlib import Path

from .security import PolicyError

FILENAME = "generation_tokens.txt"
UNKNOWN = "미집계"
MAX_TOTAL = 9_000_000_000_000_000_000


def parse_total(raw: bytes) -> int | None:
    if len(raw) > 64:
        raise PolicyError("Generation token total exceeds file limit")
    text = raw.decode("utf-8").strip()
    if text == UNKNOWN:
        return None
    if not re.fullmatch(r"0|[1-9][0-9]{0,18}", text) or int(text) > MAX_TOTAL:
        raise PolicyError("Generation token file must contain only the final integer total or 미집계")
    return int(text)


def read_total(directory: Path) -> int | None:
    path = directory / FILENAME
    if path.is_symlink():
        raise PolicyError("Generation token files cannot be symlinks")
    if not path.exists():
        return None
    with path.open("rb") as stream:
        return parse_total(stream.read(65))


def add_tokens(directory: Path, tokens: int | None, *, initial=False) -> int | None:
    path = directory / FILENAME
    if path.is_symlink():
        raise PolicyError("Generation token files cannot be symlinks")
    if initial and path.exists():
        raise PolicyError("Initial generation total already exists")
    previous = 0 if initial else read_total(directory)
    if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= MAX_TOTAL):
        raise PolicyError("Token increment must be a nonnegative reported integer")
    total = previous + tokens if previous is not None and tokens is not None else None
    if total is not None and total > MAX_TOTAL:
        raise PolicyError("Generation token total overflow")
    # Called while the publisher owns its repository lock, before staging the complete release.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(f"{total if total is not None else UNKNOWN}\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return total
