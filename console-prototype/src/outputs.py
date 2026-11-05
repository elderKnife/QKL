"""Small evidence-writing helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .common import PROJECT_ROOT


OUTPUT_ROOT = PROJECT_ROOT + "/output"


def new_run_dir(prefix: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    path = os.path.join(OUTPUT_ROOT, f"{prefix}-{stamp}")
    os.makedirs(path, exist_ok=True)
    return path


def write_json(path: str, data: dict) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def write_text(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path
