from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resolve_secret(value: str | None, env_key: str | None = None) -> str:
    if value and value.strip():
        return value.strip()
    if env_key:
        return os.environ.get(env_key, "").strip()
    return ""
