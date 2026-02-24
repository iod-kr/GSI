from __future__ import annotations

import re
import shutil
from pathlib import Path


def current_platform() -> str:
    import sys

    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized:
        raise ValueError("이름이 비어 있거나 허용되지 않은 문자만 포함되어 있습니다.")
    return normalized


def validate_port(port: int, field_name: str) -> int:
    if port < 1 or port > 65535:
        raise ValueError(f"{field_name} 포트는 1~65535 범위여야 합니다: {port}")
    return port


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def expand_home(path_value: str) -> Path:
    return Path(path_value).expanduser().resolve()
