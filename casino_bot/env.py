from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


def load_dotenv(path: str | os.PathLike[str] = ".env", *, override: bool = False) -> None:
    file_path = Path(path)
    if not file_path.is_file():
        return

    for line in file_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


__all__ = ["load_dotenv"]
