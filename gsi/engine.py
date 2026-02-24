from __future__ import annotations

import os
import queue
import re
import shlex
import shutil
import stat
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .catalog import GameDefinition, load_catalog
from .state import StateStore
from .utils import command_exists, current_platform, ensure_dir, expand_home, safe_slug, validate_port


class EngineError(Exception):
    pass


@dataclass
class JobRequest:
    job_id: str
    action: str
    payload: dict[str, Any]


class InstallerEngine:
    def __init__(self, manifest_dir: Path, data_root: str, dry_run: bool = False) -> None:
        self.platform = current_platform()
        self.manifest_dir = manifest_dir
        self.data_root = expand_home(data_root)
        self.instances_root = ensure_dir(self.data_root / "instances")
        self.backups_root = ensure_dir(self.data_root / "backups")
        self.catalog = load_catalog(manifest_dir)
        self.state = StateStore(self.data_root)
        self.dry_run = dry_run

        self._queue: queue.Queue[JobRequest] = queue.Queue()
        self._stop = threading.Event()
        self._instance_locks: dict[str, threading.Lock] = {}
        self._instance_locks_guard = threading.Lock()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._queue.put(JobRequest(job_id="", action="noop", payload={}))
        self._worker.join(timeout=2)

    def list_games(self) -> list[dict[str, Any]]:
        return [game.to_public_dict() for game in self.catalog.values()]

    def list_instances(self) -> list[dict[str, Any]]:
        instances = self.state.list_instances()
        return [instances[key] for key in sorted(instances.keys())]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.state.get_job(job_id)

    def submit_install(
        self,
        game_id: str,
        mode: str | None,
        name: str,
        port_overrides: dict[str, int] | None,
        auto_eula: bool = False,
        game_version: str | None = None,
        dependency_versions: dict[str, str] | None = None,
        install_dir: str | None = None,
        base_dir: str | None = None,
        server_folder_name: str | None = None,
    ) -> str:
        game = self.catalog.get(game_id)
        if game is None:
            raise EngineError(f"지원하지 않는 게임 ID: {game_id}")

        selected_mode = mode or game.default_mode
        if selected_mode not in game.modes:
            raise EngineError(f"지원하지 않는 모드({selected_mode}) for {game_id}")

        selected_game_version = self._resolve_game_version(game, game_version)
        selected_dependency_versions = self._resolve_dependency_versions(game, dependency_versions or {})

        slug = safe_slug(name)
        instance_id = f"{game_id}-{slug}-{int(time.time())}"
        job_id = self._new_id("job")
        payload = {
            "type": "install",
            "gameId": game_id,
            "mode": selected_mode,
            "name": name,
            "instanceId": instance_id,
            "ports": port_overrides or {},
            "autoEula": bool(auto_eula),
            "gameVersion": selected_game_version,
            "dependencyVersions": selected_dependency_versions,
            "installDir": str(install_dir or "").strip(),
            "baseDir": str(base_dir or "").strip(),
            "serverFolderName": str(server_folder_name or "").strip(),
            "submittedAt": self._now(),
            "status": "queued",
        }
        self.state.create_job(job_id, payload)
        self._queue.put(JobRequest(job_id=job_id, action="install", payload=payload))
        return job_id

    def submit_instance_action(
        self,
        instance_id: str,
        action: str,
        options: dict[str, Any] | None = None,
    ) -> str:
        if action not in {"start", "stop", "update", "backup", "restore"}:
            raise EngineError(f"지원하지 않는 액션: {action}")

        instance = self.state.get_instance(instance_id)
        if instance is None:
            raise EngineError(f"인스턴스를 찾을 수 없습니다: {instance_id}")

        job_id = self._new_id("job")
        payload = {
            "type": action,
            "instanceId": instance_id,
            "options": options or {},
            "submittedAt": self._now(),
            "status": "queued",
        }
        self.state.create_job(job_id, payload)
        self._queue.put(JobRequest(job_id=job_id, action=action, payload=payload))
        return job_id

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{int(time.time() * 1000)}"

    def _now(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            req = self._queue.get()
            if req.action == "noop":
                continue
            try:
                self._run_job(req)
            except Exception as exc:  # noqa: BLE001
                self.state.append_job_log(req.job_id, f"unexpected worker error: {exc}")
                self.state.update_job(
                    req.job_id,
                    {
                        "status": "failed",
                        "finishedAt": self._now(),
                        "error": str(exc),
                    },
                )

    def _run_job(self, req: JobRequest) -> None:
        self.state.update_job(req.job_id, {"status": "running", "startedAt": self._now()})
        handler: Callable[[str, dict[str, Any]], None]
        if req.action == "install":
            handler = self._job_install
        else:
            handler = self._job_instance_action

        try:
            handler(req.job_id, req.payload)
            self.state.update_job(req.job_id, {"status": "succeeded", "finishedAt": self._now()})
            self.state.append_job_log(req.job_id, "job completed")
        except Exception as exc:  # noqa: BLE001
            self.state.append_job_log(req.job_id, f"job failed: {exc}")
            self.state.update_job(
                req.job_id,
                {
                    "status": "failed",
                    "finishedAt": self._now(),
                    "error": str(exc),
                },
            )

    def _instance_lock(self, instance_id: str) -> threading.Lock:
        with self._instance_locks_guard:
            if instance_id not in self._instance_locks:
                self._instance_locks[instance_id] = threading.Lock()
            return self._instance_locks[instance_id]

    def _job_install(self, job_id: str, payload: dict[str, Any]) -> None:
        game = self.catalog[payload["gameId"]]
        mode_name = payload["mode"]
        mode = game.modes[mode_name]
        auto_eula = bool(payload.get("autoEula", False))
        game_version = str(payload.get("gameVersion", "")).strip() or self._resolve_game_version(game, None)
        dependency_versions = {
            str(k): str(v)
            for k, v in payload.get("dependencyVersions", {}).items()
        }

        instance_id = payload["instanceId"]
        instance_dir = self._resolve_instance_dir(payload, default_dir=self.instances_root / instance_id)
        data_dir = instance_dir / "data"
        if not self.dry_run:
            ensure_dir(instance_dir)
            ensure_dir(data_dir)
        ports = self._resolve_ports(game, mode_name, payload.get("ports", {}))

        lock = self._instance_lock(instance_id)
        with lock:
            self._preflight(job_id, mode)
            context = self._build_context(
                instance_id=instance_id,
                name=payload["name"],
                instance_dir=instance_dir,
                data_dir=data_dir,
                ports=ports,
                auto_eula=auto_eula,
                game_version=game_version,
                dependency_versions=dependency_versions,
            )

            self._download_assets(job_id, mode, context)

            if mode_name == "docker":
                compose_path = self._write_compose(job_id, game, mode, context)
                self._docker(job_id, ["compose", "-f", str(compose_path), "pull"])
                self._docker(job_id, ["compose", "-f", str(compose_path), "up", "-d"])
            else:
                self._run_native_operation(job_id, mode, "install", context)

            self._apply_auto_eula(job_id, game.game_id, data_dir, auto_eula)

            instance = {
                "id": instance_id,
                "name": payload["name"],
                "gameId": game.game_id,
                "mode": mode_name,
                "ports": ports,
                "instanceDir": str(instance_dir),
                "dataDir": str(data_dir),
                "autoEula": auto_eula,
                "gameVersion": game_version,
                "dependencyVersions": dependency_versions,
                "createdAt": self._now(),
                "updatedAt": self._now(),
            }
            if self.dry_run:
                self.state.append_job_log(job_id, f"dry-run: instance metadata skip ({instance_id})")
            else:
                self.state.upsert_instance(instance_id, instance)
                self._write_management_scripts(job_id, instance, mode)
                self.state.append_job_log(job_id, f"instance created: {instance_id}")

    def _resolve_instance_dir(self, payload: dict[str, Any], default_dir: Path) -> Path:
        install_dir_raw = str(payload.get("installDir", "")).strip()
        base_dir_raw = str(payload.get("baseDir", "")).strip()
        folder_raw = str(payload.get("serverFolderName", "")).strip()

        if install_dir_raw:
            install_dir = Path(install_dir_raw).expanduser()
            if not install_dir.is_absolute():
                raise EngineError("installDir는 절대 경로여야 합니다.")
            target = install_dir.resolve()
        elif base_dir_raw:
            base_dir = Path(base_dir_raw).expanduser()
            if not base_dir.is_absolute():
                raise EngineError("baseDir는 절대 경로여야 합니다.")
            folder = self._normalize_folder_name(folder_raw or str(payload.get("name", "server")))
            target = (base_dir / folder).resolve()
        else:
            target = default_dir.resolve()

        if not self.dry_run and target.exists() and any(target.iterdir()):
            raise EngineError(f"설치 대상 폴더가 이미 존재하고 비어 있지 않습니다: {target}")
        return target

    def _normalize_folder_name(self, folder_name: str) -> str:
        raw = folder_name.strip()
        if not raw:
            raise EngineError("서버 폴더 이름이 비어 있습니다.")
        if raw in {".", ".."}:
            raise EngineError("서버 폴더 이름이 유효하지 않습니다.")
        if "/" in raw or "\\" in raw:
            raise EngineError("서버 폴더 이름에는 경로 구분자를 사용할 수 없습니다.")
        return raw

    def _job_instance_action(self, job_id: str, payload: dict[str, Any]) -> None:
        instance_id = payload["instanceId"]
        instance = self.state.get_instance(instance_id)
        if instance is None:
            raise EngineError(f"인스턴스를 찾을 수 없습니다: {instance_id}")

        game = self.catalog[instance["gameId"]]
        mode = game.modes[instance["mode"]]

        instance_dir = Path(instance["instanceDir"])
        data_dir = Path(instance["dataDir"])
        context = self._build_context(
            instance_id=instance_id,
            name=instance["name"],
            instance_dir=instance_dir,
            data_dir=data_dir,
            ports=instance["ports"],
            auto_eula=bool(instance.get("autoEula", False)),
            game_version=str(instance.get("gameVersion", "")),
            dependency_versions={
                str(k): str(v)
                for k, v in instance.get("dependencyVersions", {}).items()
            },
        )

        action = payload["type"]
        lock = self._instance_lock(instance_id)
        with lock:
            if action == "backup":
                self._backup_instance(job_id, instance)
            elif action == "restore":
                backup_file = payload.get("options", {}).get("backupFile", "")
                if not backup_file:
                    raise EngineError("restore 액션에는 options.backupFile이 필요합니다.")
                self._restore_instance(job_id, instance, Path(backup_file))
            elif instance["mode"] == "docker":
                compose_path = instance_dir / "docker-compose.yml"
                if not compose_path.exists():
                    raise EngineError(f"docker compose 파일이 없습니다: {compose_path}")
                if action == "start":
                    self._docker(job_id, ["compose", "-f", str(compose_path), "up", "-d"])
                elif action == "stop":
                    self._docker(job_id, ["compose", "-f", str(compose_path), "stop"])
                elif action == "update":
                    self._docker(job_id, ["compose", "-f", str(compose_path), "pull"])
                    self._docker(job_id, ["compose", "-f", str(compose_path), "up", "-d"])
                else:
                    raise EngineError(f"지원하지 않는 docker 액션: {action}")
            else:
                self._run_native_operation(job_id, mode, action, context)

            instance["updatedAt"] = self._now()
            self.state.upsert_instance(instance_id, instance)

    def _resolve_ports(
        self,
        game: GameDefinition,
        mode_name: str,
        overrides: dict[str, int],
    ) -> dict[str, int]:
        defaults = game.defaults.get("ports", {})
        mode_defaults = game.modes[mode_name].get("ports", {})
        result: dict[str, int] = {}

        for key, value in {**defaults, **mode_defaults}.items():
            result[key] = validate_port(int(value), key)

        for key, value in overrides.items():
            result[key] = validate_port(int(value), key)
        return result

    def _resolve_game_version(self, game: GameDefinition, requested: str | None) -> str:
        options = game.version_options if isinstance(game.version_options, dict) else {}
        default_version = str(options.get("default", "")).strip()
        selected = (requested or default_version or "latest").strip()
        if not selected:
            selected = "latest"

        choices_raw = options.get("choices", [])
        choices = [str(item) for item in choices_raw] if isinstance(choices_raw, list) else []
        if choices and selected not in choices:
            raise EngineError(
                f"지원하지 않는 게임 버전: {selected} (허용: {', '.join(choices)})"
            )
        return selected

    def _resolve_dependency_versions(
        self,
        game: GameDefinition,
        requested: dict[str, str],
    ) -> dict[str, str]:
        options = game.dependency_options if isinstance(game.dependency_options, dict) else {}
        result: dict[str, str] = {}

        for dep_name, dep_config in options.items():
            if not isinstance(dep_config, dict):
                continue
            default_value = str(dep_config.get("default", "")).strip()
            selected_value = str(requested.get(dep_name, default_value)).strip()
            if not selected_value:
                continue

            choices_raw = dep_config.get("choices", [])
            choices = [str(item) for item in choices_raw] if isinstance(choices_raw, list) else []
            if choices and selected_value not in choices:
                raise EngineError(
                    f"{dep_name} 버전이 유효하지 않습니다: {selected_value} (허용: {', '.join(choices)})"
                )
            result[dep_name] = selected_value

        for dep_name, dep_value in requested.items():
            if dep_name not in result:
                dep_value_str = str(dep_value).strip()
                if dep_value_str:
                    result[dep_name] = dep_value_str
        return result

    def _preflight(self, job_id: str, mode: dict[str, Any]) -> None:
        for req in mode.get("requirements", []):
            if not command_exists(req):
                raise EngineError(f"필수 명령어를 찾을 수 없습니다: {req}")
            self.state.append_job_log(job_id, f"preflight ok: {req}")

    def _build_context(
        self,
        instance_id: str,
        name: str,
        instance_dir: Path,
        data_dir: Path,
        ports: dict[str, int],
        auto_eula: bool,
        game_version: str,
        dependency_versions: dict[str, str],
    ) -> dict[str, str]:
        context: dict[str, str] = {
            "instance_id": instance_id,
            "server_name": name,
            "instance_dir": str(instance_dir),
            "data_dir": str(data_dir),
            "backup_dir": str(self.backups_root),
            "auto_eula": "true" if auto_eula else "false",
            "minecraft_eula": "TRUE" if auto_eula else "FALSE",
            "palworld_eula": "true" if auto_eula else "false",
            "game_version": game_version,
        }
        for dep_name, dep_version in dependency_versions.items():
            normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", dep_name).strip("_").lower()
            if not normalized:
                continue
            context[f"dep_{normalized}_version"] = dep_version
        for key, value in ports.items():
            context[f"{key}_port"] = str(value)
        return context

    def _download_assets(self, job_id: str, mode: dict[str, Any], context: dict[str, str]) -> None:
        downloads = mode.get("downloads", [])
        if not isinstance(downloads, list):
            return

        for item in downloads:
            if isinstance(item, str):
                url = self._render_template(item, context)
                file_name = Path(url).name or "download.bin"
                target = Path(context["data_dir"]) / file_name
                executable = False
            elif isinstance(item, dict):
                url = self._render_template(str(item.get("url", "")), context)
                if not url:
                    raise EngineError("downloads.url 값이 비어 있습니다.")
                raw_target = str(item.get("target", ""))
                if not raw_target:
                    file_name = Path(url).name or "download.bin"
                    target = Path(context["data_dir"]) / file_name
                else:
                    target = Path(self._render_template(raw_target, context))
                executable = bool(item.get("executable", False))
            else:
                raise EngineError("downloads 항목은 string 또는 object여야 합니다.")

            ensure_dir(target.parent)
            self.state.append_job_log(job_id, f"download: {url} -> {target}")
            if self.dry_run:
                continue

            try:
                urllib.request.urlretrieve(url, target)
            except urllib.error.URLError as exc:
                raise EngineError(f"다운로드 실패: {url} ({exc})") from exc

            if executable:
                mode_bits = target.stat().st_mode
                target.chmod(mode_bits | stat.S_IXUSR | stat.S_IXGRP)

    def _apply_auto_eula(self, job_id: str, game_id: str, data_dir: Path, auto_eula: bool) -> None:
        if not auto_eula:
            self.state.append_job_log(job_id, "auto EULA disabled")
            return
        if self.dry_run:
            self.state.append_job_log(job_id, f"dry-run: auto EULA skip ({game_id})")
            return

        marker = data_dir / ".gsi-eula-accepted"
        marker.write_text(
            f"accepted_at={self._now()}\ngame={game_id}\n",
            encoding="utf-8",
        )
        self.state.append_job_log(job_id, f"eula marker written: {marker}")

        if game_id == "minecraft":
            eula_path = data_dir / "eula.txt"
            eula_path.write_text("eula=true\n", encoding="utf-8")
            self.state.append_job_log(job_id, f"minecraft eula written: {eula_path}")

    def _write_compose(
        self,
        job_id: str,
        game: GameDefinition,
        mode: dict[str, Any],
        context: dict[str, str],
    ) -> Path:
        instance_dir = Path(context["instance_dir"])
        compose_path = instance_dir / "docker-compose.yml"

        image = self._render_template(str(mode.get("image", "")), context)
        if not image:
            raise EngineError(f"docker image 누락: {game.game_id}")

        env = mode.get("env", {})
        volumes = mode.get("volumes", [])
        port_map = mode.get("port_map", {})

        lines = [
            "services:",
            "  server:",
            f"    image: {image}",
            f"    container_name: gsi-{context['instance_id']}",
            "    restart: unless-stopped",
        ]

        if env:
            lines.append("    environment:")
            for key, value in env.items():
                rendered = self._render_template(str(value), context)
                lines.append(f"      {key}: \"{rendered}\"")

        if port_map:
            lines.append("    ports:")
            for name, container_port in port_map.items():
                host_key = f"{name}_port"
                host_port = context.get(host_key)
                if host_port is None:
                    continue
                lines.append(f"      - \"{host_port}:{container_port}\"")

        if volumes:
            lines.append("    volumes:")
            for volume in volumes:
                rendered = self._render_template(str(volume), context)
                lines.append(f"      - {rendered}")

        if self.dry_run:
            self.state.append_job_log(job_id, f"dry-run: compose generation skip ({compose_path})")
        else:
            compose_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.state.append_job_log(job_id, f"compose file written: {compose_path}")
        return compose_path

    def _docker(self, job_id: str, args: list[str]) -> None:
        cmd = ["docker", *args]
        self.state.append_job_log(job_id, f"run: {' '.join(shlex.quote(x) for x in cmd)}")
        if self.dry_run:
            return
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        output = proc.stdout.strip()
        if output:
            self.state.append_job_log(job_id, output)
        if proc.returncode != 0:
            raise EngineError(f"명령 실행 실패({proc.returncode}): {' '.join(cmd)}")

    def _run_native_operation(
        self,
        job_id: str,
        mode: dict[str, Any],
        action: str,
        context: dict[str, str],
    ) -> None:
        commands = mode.get("commands", {}).get(action, {})
        selected = commands.get(self.platform, [])
        if not selected:
            self.state.append_job_log(job_id, f"native {action} command not defined for {self.platform}")
            return

        for command in selected:
            rendered = self._render_template(command, context)
            self.state.append_job_log(job_id, f"run: {rendered}")
            if self.dry_run:
                continue
            proc = subprocess.run(
                rendered,
                shell=True,
                cwd=context["instance_dir"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            if proc.stdout.strip():
                self.state.append_job_log(job_id, proc.stdout.strip())
            if proc.returncode != 0:
                raise EngineError(f"네이티브 명령 실패({proc.returncode}): {rendered}")

    def _write_management_scripts(
        self,
        job_id: str,
        instance: dict[str, Any],
        mode: dict[str, Any],
    ) -> None:
        instance_id = instance["id"]
        instance_dir = Path(instance["instanceDir"])
        mode_name = instance["mode"]
        data_root = str(self.data_root)

        shell_scripts: dict[str, str] = {}
        cmd_scripts: dict[str, str] = {}

        compose_path = instance_dir / "docker-compose.yml"
        if mode_name == "docker":
            compose_q = shlex.quote(str(compose_path))
            shell_scripts = {
                "start.sh": f"#!/usr/bin/env bash\nset -euo pipefail\ndocker compose -f {compose_q} up -d\n",
                "stop.sh": f"#!/usr/bin/env bash\nset -euo pipefail\ndocker compose -f {compose_q} stop\n",
                "update.sh": f"#!/usr/bin/env bash\nset -euo pipefail\ndocker compose -f {compose_q} pull\ndocker compose -f {compose_q} up -d\n",
            }
            cmd_scripts = {
                "start.cmd": f"@echo off\ndocker compose -f \"{compose_path}\" up -d\n",
                "stop.cmd": f"@echo off\ndocker compose -f \"{compose_path}\" stop\n",
                "update.cmd": f"@echo off\ndocker compose -f \"{compose_path}\" pull\ndocker compose -f \"{compose_path}\" up -d\n",
            }
        else:
            native_commands = mode.get("commands", {})
            shell_scripts = {
                "start.sh": self._native_shell_script(native_commands, "start", self.platform, instance),
                "stop.sh": self._native_shell_script(native_commands, "stop", self.platform, instance),
                "update.sh": self._native_shell_script(native_commands, "update", self.platform, instance),
            }
            cmd_scripts = {
                "start.cmd": self._native_cmd_script(native_commands, "start", "windows", instance),
                "stop.cmd": self._native_cmd_script(native_commands, "stop", "windows", instance),
                "update.cmd": self._native_cmd_script(native_commands, "update", "windows", instance),
            }

        shell_scripts["backup.sh"] = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f"python3 -m gsi --data-root {shlex.quote(data_root)} backup --instance {shlex.quote(instance_id)}\n"
        )
        shell_scripts["restore.sh"] = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "if [ $# -lt 1 ]; then\n"
            "  echo \"Usage: ./restore.sh <backup-file.tar.gz>\"\n"
            "  exit 1\n"
            "fi\n"
            f"python3 -m gsi --data-root {shlex.quote(data_root)} restore --instance {shlex.quote(instance_id)} --backup-file \"$1\"\n"
        )

        cmd_scripts["backup.cmd"] = (
            "@echo off\n"
            f"python -m gsi --data-root \"{data_root}\" backup --instance \"{instance_id}\"\n"
        )
        cmd_scripts["restore.cmd"] = (
            "@echo off\n"
            "if \"%~1\"==\"\" (\n"
            "  echo Usage: restore.cmd ^<backup-file.tar.gz^>\n"
            "  exit /b 1\n"
            ")\n"
            f"python -m gsi --data-root \"{data_root}\" restore --instance \"{instance_id}\" --backup-file \"%~1\"\n"
        )

        for file_name, content in shell_scripts.items():
            path = instance_dir / file_name
            path.write_text(content, encoding="utf-8")
            if not self.dry_run:
                mode_bits = path.stat().st_mode
                path.chmod(mode_bits | stat.S_IXUSR)

        for file_name, content in cmd_scripts.items():
            path = instance_dir / file_name
            path.write_text(content, encoding="utf-8")

        self.state.append_job_log(job_id, f"management scripts written: {instance_dir}")

    def _native_shell_script(
        self,
        commands: dict[str, Any],
        action: str,
        platform: str,
        instance: dict[str, Any],
    ) -> str:
        selected = commands.get(action, {}).get(platform, [])
        if not selected:
            return (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"echo 'No native {action} command for {platform}'\n"
            )
        context = self._build_context(
            instance_id=instance["id"],
            name=instance["name"],
            instance_dir=Path(instance["instanceDir"]),
            data_dir=Path(instance["dataDir"]),
            ports=instance["ports"],
            auto_eula=bool(instance.get("autoEula", False)),
            game_version=str(instance.get("gameVersion", "")),
            dependency_versions={
                str(k): str(v)
                for k, v in instance.get("dependencyVersions", {}).items()
            },
        )
        rendered = [self._render_template(str(line), context) for line in selected]
        body = "\n".join(rendered)
        return f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n"

    def _native_cmd_script(
        self,
        commands: dict[str, Any],
        action: str,
        platform: str,
        instance: dict[str, Any],
    ) -> str:
        selected = commands.get(action, {}).get(platform, [])
        if not selected:
            return f"@echo off\necho No native {action} command for {platform}\n"
        context = self._build_context(
            instance_id=instance["id"],
            name=instance["name"],
            instance_dir=Path(instance["instanceDir"]),
            data_dir=Path(instance["dataDir"]),
            ports=instance["ports"],
            auto_eula=bool(instance.get("autoEula", False)),
            game_version=str(instance.get("gameVersion", "")),
            dependency_versions={
                str(k): str(v)
                for k, v in instance.get("dependencyVersions", {}).items()
            },
        )
        rendered = [self._render_template(str(line), context) for line in selected]
        body = "\n".join(rendered)
        return f"@echo off\n{body}\n"

    def _render_template(self, template: str, context: dict[str, str]) -> str:
        rendered = template
        for key, value in context.items():
            rendered = rendered.replace("{" + key + "}", value)
        return rendered

    def _backup_instance(self, job_id: str, instance: dict[str, Any]) -> None:
        instance_id = instance["id"]
        source = Path(instance["instanceDir"])
        if not source.exists():
            raise EngineError(f"백업 대상 디렉터리가 없습니다: {source}")

        backup_name = f"{instance_id}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}.tar.gz"
        target = self.backups_root / backup_name

        self.state.append_job_log(job_id, f"backup start: {source} -> {target}")
        if self.dry_run:
            return

        with tarfile.open(target, "w:gz") as tar:
            tar.add(source, arcname=instance_id)
        self.state.append_job_log(job_id, f"backup done: {target}")

    def _restore_instance(self, job_id: str, instance: dict[str, Any], backup_file: Path) -> None:
        if not backup_file.exists():
            raise EngineError(f"백업 파일이 없습니다: {backup_file}")

        instance_id = instance["id"]
        target = Path(instance["instanceDir"])

        self.state.append_job_log(job_id, f"restore start: {backup_file} -> {target}")
        if self.dry_run:
            return

        if target.exists():
            shutil.rmtree(target)
        ensure_dir(target.parent)

        with tarfile.open(backup_file, "r:gz") as tar:
            self._safe_extract_tar(tar, target.parent)

        extracted = target.parent / instance_id
        if extracted != target and extracted.exists():
            if target.exists():
                shutil.rmtree(target)
            extracted.replace(target)
        self.state.append_job_log(job_id, "restore done")

    def _safe_extract_tar(self, tar: tarfile.TarFile, destination: Path) -> None:
        destination_resolved = destination.resolve()
        for member in tar.getmembers():
            member_path = (destination / member.name).resolve()
            if not os.path.commonpath([str(destination_resolved), str(member_path)]) == str(destination_resolved):
                raise EngineError(f"위험한 tar 경로가 감지되었습니다: {member.name}")
        tar.extractall(destination)
