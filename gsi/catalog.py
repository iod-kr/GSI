from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class GameDefinition:
    game_id: str
    name: str
    description: str
    default_mode: str
    defaults: dict[str, Any]
    modes: dict[str, dict[str, Any]]
    version_options: dict[str, Any]
    dependency_options: dict[str, Any]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.game_id,
            "name": self.name,
            "description": self.description,
            "defaultMode": self.default_mode,
            "defaults": self.defaults,
            "modes": list(self.modes.keys()),
            "versionOptions": self.version_options,
            "dependencyOptions": self.dependency_options,
        }


class CatalogError(Exception):
    pass


def _read_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CatalogError(f"잘못된 매니페스트 형식: {path}")
    return raw


def _validate_manifest(path: Path, payload: dict[str, Any]) -> GameDefinition:
    required = ["id", "name", "default_mode", "modes"]
    for key in required:
        if key not in payload:
            raise CatalogError(f"필수 키 누락({key}): {path}")

    modes = payload["modes"]
    if not isinstance(modes, dict) or not modes:
        raise CatalogError(f"modes는 비어 있지 않은 object여야 합니다: {path}")

    default_mode = payload["default_mode"]
    if default_mode not in modes:
        raise CatalogError(
            f"default_mode({default_mode})가 modes에 없습니다: {path}"
        )

    return GameDefinition(
        game_id=str(payload["id"]),
        name=str(payload["name"]),
        description=str(payload.get("description", "")),
        default_mode=str(default_mode),
        defaults=payload.get("defaults", {}) if isinstance(payload.get("defaults", {}), dict) else {},
        modes=modes,
        version_options=(
            payload.get("version_options", {})
            if isinstance(payload.get("version_options", {}), dict)
            else {}
        ),
        dependency_options=(
            payload.get("dependency_options", {})
            if isinstance(payload.get("dependency_options", {}), dict)
            else {}
        ),
    )


def load_catalog(manifest_dir: Path) -> dict[str, GameDefinition]:
    if not manifest_dir.exists():
        raise CatalogError(f"매니페스트 디렉터리가 없습니다: {manifest_dir}")

    catalog: dict[str, GameDefinition] = {}
    for file_path in sorted(manifest_dir.glob("*.yaml")):
        game = _validate_manifest(file_path, _read_yaml(file_path))
        if game.game_id in catalog:
            raise CatalogError(f"중복 게임 ID: {game.game_id}")
        catalog[game.game_id] = game

    if not catalog:
        raise CatalogError("로드할 게임 매니페스트가 없습니다.")

    return catalog
