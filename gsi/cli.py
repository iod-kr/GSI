from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import __version__
from .engine import EngineError, InstallerEngine

INSTALLER_UPDATE_URL = "https://raw.githubusercontent.com/project-gsi/gsi-installer/main/installer-version.json"
GSI_GITHUB_REPO_URL = "https://github.com/project-gsi/gsi-installer.git"

EULA_GUIDE: dict[str, str] = {
    "minecraft": (
        "Minecraft EULA 동의가 필요합니다. 자동 동의 선택 시 eula.txt에 eula=true가 기록됩니다."
    ),
    "valheim": "Valheim 서버 운영 시 게임/서비스 약관을 확인하고 동의 후 진행하세요.",
    "cs2": "Counter-Strike 2 서버 운영 정책 및 Steam 약관을 확인한 뒤 진행하세요.",
    "palworld": "Palworld 서버 운영 시 Pocketpair 및 플랫폼 약관을 확인하고 동의하세요.",
}

LINUX_PACKAGE_MAP: dict[str, dict[str, str]] = {
    "apt-get": {
        "python": "python3",
        "docker": "docker.io",
        "java": "default-jre-headless",
        "steamcmd": "steamcmd",
        "curl": "curl",
    },
    "dnf": {
        "python": "python3",
        "docker": "docker",
        "java": "java-21-openjdk-headless",
        "steamcmd": "",
        "curl": "curl",
    },
    "yum": {
        "python": "python3",
        "docker": "docker",
        "java": "java-17-openjdk-headless",
        "steamcmd": "",
        "curl": "curl",
    },
    "pacman": {
        "python": "python",
        "docker": "docker",
        "java": "jre-openjdk",
        "steamcmd": "",
        "curl": "curl",
    },
    "zypper": {
        "python": "python3",
        "docker": "docker",
        "java": "java-21-openjdk-headless",
        "steamcmd": "",
        "curl": "curl",
    },
}

WINDOWS_WINGET_MAP: dict[str, str] = {
    "python": "Python.Python.3.12",
    "docker": "Docker.DockerDesktop",
    "java": "EclipseAdoptium.Temurin.21.JRE",
    "steamcmd": "Valve.SteamCMD",
    "curl": "cURL.cURL",
}

_APT_UPDATED = False


def parse_ports(raw: str) -> dict[str, int]:
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("ports는 JSON object여야 합니다.")
    return {str(k): int(v) for k, v in payload.items()}


def parse_dep_versions(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("dep-versions는 JSON object여야 합니다.")
    return {str(k): str(v) for k, v in payload.items()}


def print_banner() -> None:
    print("=" * 72)
    print(" GSI Installer CLI")
    print(" Interactive Game Server Installer (Windows CMD / Linux Terminal)")
    print("=" * 72)


def print_step(number: int, title: str) -> None:
    print(f"\n[{number}. {title}]")


def choose_index(prompt: str, total: int, default_index: int = 0) -> int:
    if total <= 0:
        raise ValueError("선택 가능한 항목이 없습니다.")
    default_number = default_index + 1
    while True:
        raw = input(f"{prompt} [기본 {default_number}]: ").strip()
        if not raw:
            return default_index
        if raw.isdigit():
            number = int(raw)
            if 1 <= number <= total:
                return number - 1
        print(f"1~{total} 범위의 숫자를 입력하세요.")


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("y 또는 n으로 입력하세요.")


def ensure_admin_privileges() -> None:
    if os.name == "nt":
        ok, _ = _run_command(["net", "session"])
        if not ok:
            ok, _ = _run_command(["fltmc"])
        if not ok:
            raise EngineError(
                "[E0001] 관리자 권한이 필요합니다. "
                "CMD/PowerShell을 '관리자 권한으로 실행' 후 다시 시도하세요."
            )
        return

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise EngineError(
            "[E0001] 관리자 권한(root)이 필요합니다. "
            "sudo 또는 root 쉘에서 다시 실행하세요."
        )


def _find_available_command(candidates: list[str]) -> str | None:
    for cmd in candidates:
        path = shutil.which(cmd)
        if path:
            return path
    return None


def _detect_linux_package_manager() -> str | None:
    for pm in ["apt-get", "dnf", "yum", "pacman", "zypper"]:
        if shutil.which(pm):
            return pm
    return None


def _build_install_commands_for_dependency(dep_name: str) -> tuple[list[list[str]], str]:
    global _APT_UPDATED

    if os.name == "nt":
        winget = shutil.which("winget")
        pkg = WINDOWS_WINGET_MAP.get(dep_name, "")
        if winget and pkg:
            return (
                [
                    [
                        winget,
                        "install",
                        "--id",
                        pkg,
                        "--exact",
                        "--silent",
                        "--accept-source-agreements",
                        "--accept-package-agreements",
                    ]
                ],
                "winget",
            )
        return ([], "unsupported")

    pm = _detect_linux_package_manager()
    if not pm:
        return ([], "unsupported")

    pkg = LINUX_PACKAGE_MAP.get(pm, {}).get(dep_name, "")
    if not pkg:
        return ([], pm)

    if pm == "apt-get":
        commands: list[list[str]] = []
        if not _APT_UPDATED:
            commands.append([pm, "update"])
        commands.append([pm, "install", "-y", pkg])
        return (commands, pm)
    if pm in {"dnf", "yum"}:
        return ([[pm, "install", "-y", pkg]], pm)
    if pm == "pacman":
        return ([[pm, "-Sy", "--noconfirm", pkg]], pm)
    if pm == "zypper":
        return ([[pm, "--non-interactive", "install", pkg]], pm)
    return ([], pm)


def _attempt_dependency_install(dep_name: str) -> tuple[bool, str]:
    global _APT_UPDATED

    commands, manager = _build_install_commands_for_dependency(dep_name)
    if not commands:
        if manager == "unsupported":
            return (
                False,
                f"[E2103] {dep_name}: 자동 설치를 위한 패키지 매니저(또는 레시피)를 찾지 못했습니다.",
            )
        return (
            False,
            f"[E2103] {dep_name}: {manager} 환경에서 자동 설치 레시피가 없습니다.",
        )

    for cmd in commands:
        rendered = " ".join(cmd)
        print(f"- 실행: {rendered}")
        ok, output = _run_with_optional_sudo(cmd) if os.name != "nt" else _run_command(cmd)
        if not ok:
            detail = output.strip() or "no output"
            return (
                False,
                f"[E2102] {dep_name} 자동 설치 실패 ({manager})\\n{detail}",
            )
        if cmd[:2] == ["apt-get", "update"]:
            _APT_UPDATED = True
    return (True, f"[OK] {dep_name} 자동 설치 성공 ({manager})")


def wait_for_job(engine: InstallerEngine, job_id: str, poll_sec: float = 0.8) -> int:
    last_log = ""
    while True:
        job = engine.get_job(job_id)
        if not job:
            print(f"[ERROR][E7002] job not found: {job_id}")
            return 2

        log = job.get("log", "")
        if log != last_log:
            print(log[len(last_log) :], end="")
            last_log = log

        status = job.get("status")
        if status in {"succeeded", "failed"}:
            if status == "succeeded":
                print(f"\n[OK] job {job_id} completed")
                return 0
            print(f"\n[FAILED] {job.get('error', 'unknown error')}")
            return 1

        time.sleep(poll_sec)


def _check_installer_update() -> None:
    print_step(1, "설치기 업데이트 확인")
    print(f"- 고정 GitHub 저장소: {GSI_GITHUB_REPO_URL}")
    print(f"- 고정 GitHub 주소: {INSTALLER_UPDATE_URL}")
    print(f"- 현재 설치기 버전: {__version__}")
    try:
        with urllib.request.urlopen(INSTALLER_UPDATE_URL, timeout=5) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print("[WARN][E1002] 업데이트 응답 형식이 올바르지 않습니다.")
            return

        latest = str(payload.get("version", "")).strip()
        if not latest:
            print("[WARN][E1003] 원격 버전 정보가 비어 있습니다.")
            return

        if latest == __version__:
            print(f"[OK] 최신 버전입니다. ({latest})")
        else:
            notes = str(payload.get("notes", "")).strip()
            print(f"[WARN][E1004] 업데이트 가능: 현재={__version__}, 최신={latest}")
            if notes:
                print(f"- 릴리스 노트: {notes}")
    except urllib.error.URLError as exc:
        print(f"[WARN][E1001] 업데이트 확인 실패(네트워크/주소): {exc}")
    except json.JSONDecodeError as exc:
        print(f"[WARN][E1005] 업데이트 응답 JSON 파싱 실패: {exc}")


def _check_sdk_dependencies() -> None:
    print_step(2, "필요 SDK/의존성 확인")
    checks = [
        ("python", ["python3", "python"], True),
        ("docker", ["docker"], False),
        ("java", ["java"], False),
        ("steamcmd", ["steamcmd"], False),
        ("curl", ["curl"], False),
    ]

    missing_required: list[tuple[str, list[str]]] = []
    missing_optional: list[tuple[str, list[str]]] = []

    for name, candidates, required in checks:
        found = _find_available_command(candidates)
        if found:
            print(f"[OK] {name}: {found}")
        else:
            if required:
                print(f"[ERROR][E2001] 필수 의존성 누락: {name}")
                missing_required.append((name, candidates))
            else:
                print(
                    f"[WARN][E2002] 선택 의존성 누락: {name} (선택한 게임/모드에서 필요 시 설치 실패 가능)"
                )
                missing_optional.append((name, candidates))

    if not missing_required and not missing_optional:
        print("[OK] 의존성 점검 완료: 누락 항목 없음")
        return

    missing_names = [name for name, _ in [*missing_required, *missing_optional]]
    print(f"- 누락 의존성: {', '.join(missing_names)}")

    default_auto_install = bool(missing_required)
    if not prompt_yes_no("누락 의존성 자동 설치를 시도할까요?", default_auto_install):
        if missing_required:
            raise EngineError("[E2003] 필수 의존성 누락 상태로 설치를 계속할 수 없습니다.")
        print("[INFO] 선택 의존성 자동 설치를 건너뜁니다.")
        return

    for name, _ in [*missing_required, *missing_optional]:
        required = any(req_name == name for req_name, _ in missing_required)
        default_choice = True if required else False
        if not prompt_yes_no(f"{name} 자동 설치를 진행할까요?", default_choice):
            if required:
                raise EngineError(
                    f"[E2004] 필수 의존성({name}) 자동 설치 거부로 설치를 중단합니다."
                )
            print(f"[INFO] 선택 의존성({name}) 자동 설치를 건너뜁니다.")
            continue

        ok, detail = _attempt_dependency_install(name)
        if ok:
            print(detail)
        else:
            print(f"[ERROR] {detail}")
            if required:
                raise EngineError(
                    f"[E2005] 필수 의존성({name}) 자동 설치 실패로 설치를 중단합니다."
                )

    unresolved_required: list[str] = []
    unresolved_optional: list[str] = []
    for name, candidates, required in checks:
        if _find_available_command(candidates):
            continue
        if required:
            unresolved_required.append(name)
        else:
            unresolved_optional.append(name)

    if unresolved_required:
        raise EngineError(
            f"[E2006] 필수 의존성 재검증 실패: {', '.join(unresolved_required)}"
        )

    if unresolved_optional:
        print(
            f"[WARN][E2007] 선택 의존성 미설치: {', '.join(unresolved_optional)} "
            "(선택한 게임/모드에 따라 이후 실패 가능)"
        )
    else:
        print("[OK] 누락 의존성 자동 설치/재검증 완료")


def _step_select_game(games: list[dict[str, Any]]) -> dict[str, Any]:
    print_step(3, "게임 목록")
    for idx, game in enumerate(games, start=1):
        print(f"{idx}. {game['name']}")
    selected = games[choose_index("설치할 게임 번호", len(games), 0)]
    print(f"- 선택됨: {selected['name']}")
    return selected


def _step_select_path() -> tuple[str | None, str | None, bool]:
    print_step(4, "경로 지정")
    print("1. 전체 풀 경로 직접 입력")
    print("2. 위치 경로만 입력")
    choice = choose_index("경로 방식 번호", 2, 0)

    if choice == 0:
        install_dir = input("설치 전체 경로(절대 경로) 입력: ").strip()
        if not install_dir:
            raise EngineError("[E4001] 설치 전체 경로가 비어 있습니다.")
        if not Path(install_dir).expanduser().is_absolute():
            raise EngineError("[E4002] 설치 전체 경로는 절대 경로여야 합니다.")
        return str(Path(install_dir).expanduser()), None, True

    base_dir = input("설치 위치 경로(절대 경로) 입력: ").strip()
    if not base_dir:
        raise EngineError("[E4003] 설치 위치 경로가 비어 있습니다.")
    if not Path(base_dir).expanduser().is_absolute():
        raise EngineError("[E4004] 설치 위치 경로는 절대 경로여야 합니다.")
    return None, str(Path(base_dir).expanduser()), False


def _step_server_folder(path_is_full: bool, install_dir: str | None) -> tuple[str, str | None]:
    print_step(5, "서버 폴더 이름")

    if path_is_full:
        full_path = Path(str(install_dir)).expanduser()
        default_name = full_path.name or "server"
        folder_name = input(f"서버 폴더 이름(기본: {default_name}): ").strip() or default_name
        final_path = str(full_path.parent / folder_name)
        print(f"- 최종 설치 경로: {final_path}")
        return folder_name, final_path

    folder_name = input("서버 폴더 이름 입력 (예: my-game-server): ").strip()
    if not folder_name:
        raise EngineError("[E5001] 서버 폴더 이름이 비어 있습니다.")
    return folder_name, None


def _resolve_mode_and_versions(game: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    modes = list(game["modes"])
    default_mode = str(game.get("defaultMode", modes[0]))
    default_mode_index = modes.index(default_mode) if default_mode in modes else 0
    print("- 모드 선택")
    for idx, mode_name in enumerate(modes, start=1):
        marker = " (default)" if idx - 1 == default_mode_index else ""
        print(f"  {idx}. {mode_name}{marker}")
    mode = modes[choose_index("모드 번호", len(modes), default_mode_index)]

    version_opts = game.get("versionOptions", {}) if isinstance(game.get("versionOptions", {}), dict) else {}
    default_game_version = str(version_opts.get("default", "latest")) or "latest"
    choices = version_opts.get("choices", [])
    if isinstance(choices, list) and choices:
        default_ver_index = choices.index(default_game_version) if default_game_version in choices else 0
        print("- 게임 버전 선택")
        for idx, item in enumerate(choices, start=1):
            marker = " (default)" if idx - 1 == default_ver_index else ""
            print(f"  {idx}. {item}{marker}")
        game_version = str(choices[choose_index("게임 버전 번호", len(choices), default_ver_index)])
    else:
        game_version = input(f"게임 버전 (기본: {default_game_version}): ").strip() or default_game_version

    dependency_versions: dict[str, str] = {}
    dep_opts = game.get("dependencyOptions", {}) if isinstance(game.get("dependencyOptions", {}), dict) else {}
    if dep_opts:
        print("- 추가 종속성 버전 선택")
        for dep_name, dep_cfg in dep_opts.items():
            if not isinstance(dep_cfg, dict):
                continue
            dep_default = str(dep_cfg.get("default", "")).strip()
            dep_choices = dep_cfg.get("choices", [])
            dep_value = dep_default
            if isinstance(dep_choices, list) and dep_choices:
                default_dep_index = dep_choices.index(dep_default) if dep_default in dep_choices else 0
                print(f"  * {dep_name}")
                for idx, item in enumerate(dep_choices, start=1):
                    marker = " (default)" if idx - 1 == default_dep_index else ""
                    print(f"    {idx}. {item}{marker}")
                dep_value = str(dep_choices[choose_index(f"{dep_name} 번호", len(dep_choices), default_dep_index)])
            else:
                dep_value = input(f"  {dep_name} 버전 (기본: {dep_default or '없음'}): ").strip() or dep_default
            if dep_value:
                dependency_versions[dep_name] = dep_value

    return mode, game_version, dependency_versions


def _step_eula(game_id: str) -> bool:
    print_step(6, "EULA 동의")
    guide = EULA_GUIDE.get(game_id, "게임/서비스 약관을 확인하고 동의 후 진행하세요.")
    print(guide)
    agreed = prompt_yes_no("위 내용을 확인했고 자동 동의 옵션으로 진행하시겠습니까?", False)
    if not agreed:
        print("[INFO] EULA 미동의로 설치를 중단합니다.")
    return agreed


def _run_command(command: list[str]) -> tuple[bool, str]:
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    return proc.returncode == 0, proc.stdout.strip()


def _run_with_optional_sudo(command: list[str]) -> tuple[bool, str]:
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0 and shutil.which("sudo"):
        return _run_command(["sudo", *command])
    return _run_command(command)


def _step_network_open(instance: dict[str, Any] | None) -> None:
    print_step(8, "네트워크 개방 작업")
    if not instance:
        print("[WARN][E8000] 설치 결과 인스턴스를 찾지 못해 네트워크 개방 단계를 건너뜁니다.")
        return

    ports = instance.get("ports", {}) if isinstance(instance.get("ports", {}), dict) else {}
    port_list = sorted({int(v) for v in ports.values()})
    if not port_list:
        print("[WARN][E8004] 개방할 포트가 없습니다.")
        return

    print(f"- 대상 포트: {port_list}")
    if not prompt_yes_no("자동으로 포트 개방 작업을 시도할까요?", True):
        print("[INFO] 사용자가 포트 개방 자동 작업을 건너뛰었습니다.")
        return

    if os.name == "nt":
        if not shutil.which("netsh"):
            print("[ERROR][E8001] netsh를 찾을 수 없어 포트 개방을 진행할 수 없습니다.")
            return
        for port in port_list:
            for protocol in ("TCP", "UDP"):
                rule_name = f"GSI-{instance.get('id', 'server')}-{port}-{protocol}"
                ok, output = _run_command(
                    [
                        "netsh",
                        "advfirewall",
                        "firewall",
                        "add",
                        "rule",
                        f"name={rule_name}",
                        "dir=in",
                        "action=allow",
                        f"protocol={protocol}",
                        f"localport={port}",
                    ]
                )
                if ok:
                    print(f"[OK] 방화벽 규칙 추가: {rule_name}")
                else:
                    print(f"[ERROR][E8002] 방화벽 규칙 추가 실패: {rule_name}")
                    if output:
                        print(output)
        print("[INFO] Windows 방화벽 개방 작업이 끝났습니다.")
        return

    if shutil.which("ufw"):
        for port in port_list:
            for proto in ("tcp", "udp"):
                ok, output = _run_with_optional_sudo(["ufw", "allow", f"{port}/{proto}"])
                if ok:
                    print(f"[OK] ufw allow {port}/{proto}")
                else:
                    print(f"[ERROR][E8002] ufw 규칙 추가 실패: {port}/{proto}")
                    if output:
                        print(output)
        return

    if shutil.which("firewall-cmd"):
        for port in port_list:
            for proto in ("tcp", "udp"):
                ok, output = _run_with_optional_sudo(
                    ["firewall-cmd", "--permanent", "--add-port", f"{port}/{proto}"]
                )
                if ok:
                    print(f"[OK] firewall-cmd add-port {port}/{proto}")
                else:
                    print(f"[ERROR][E8002] firewall-cmd 규칙 추가 실패: {port}/{proto}")
                    if output:
                        print(output)
        ok, output = _run_with_optional_sudo(["firewall-cmd", "--reload"])
        if ok:
            print("[OK] firewall-cmd reload")
        else:
            print("[ERROR][E8003] firewall-cmd reload 실패")
            if output:
                print(output)
        return

    print("[ERROR][E8001] 지원되는 방화벽 도구(ufw/firewall-cmd/netsh)를 찾을 수 없습니다.")


def _check_local_tcp_open(port: int, timeout_sec: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _step_port_check(instance: dict[str, Any] | None) -> None:
    print_step(9, "외부 접속 테스트/포트 체크")
    if not instance:
        print("[WARN][E9000] 설치 결과 인스턴스를 찾지 못해 포트 체크를 건너뜁니다.")
        return

    ports = instance.get("ports", {}) if isinstance(instance.get("ports", {}), dict) else {}
    port_list = sorted({int(v) for v in ports.values()})
    if not port_list:
        print("[WARN][E9001] 확인할 포트가 없습니다.")
        return

    for port in port_list:
        status = "OPEN" if _check_local_tcp_open(port) else "CLOSED/NOT_LISTENING"
        print(f"- localhost:{port} -> {status}")

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as response:
            public_ip = response.read().decode("utf-8").strip()
        if public_ip:
            preview = ", ".join(str(p) for p in port_list)
            print(f"- 외부 접속 확인용 공인 IP: {public_ip} (포트: {preview})")
    except urllib.error.URLError as exc:
        print(f"[WARN][E9002] 공인 IP 조회 실패: {exc}")


def _notify_windows_ready(message: str) -> None:
    if shutil.which("msg"):
        _run_command(["msg", "*", message])
        return
    if shutil.which("powershell"):
        _run_command(["powershell", "-NoProfile", "-Command", f"Write-Host '{message}'"])


def _step_finish(instance: dict[str, Any] | None) -> None:
    print_step(10, "서버 오픈 알림/서버 쉘")
    if not instance:
        print("[WARN][E10001] 설치 결과 인스턴스를 찾지 못해 최종 단계를 축약합니다.")
        return

    msg = (
        f"서버 준비 완료: id={instance.get('id')} "
        f"game={instance.get('gameId')} mode={instance.get('mode')}"
    )
    print(f"[OK] {msg}")

    if os.name == "nt":
        _notify_windows_ready(f"GSI: {msg}")

    if not prompt_yes_no("서버 폴더 쉘을 지금 열까요?", False):
        return

    instance_dir = str(instance.get("instanceDir", "")).strip()
    if not instance_dir:
        print("[WARN][E10002] instanceDir가 없어 쉘을 열 수 없습니다.")
        return

    try:
        if os.name == "nt":
            subprocess.call(["cmd", "/K"], cwd=instance_dir)
        else:
            shell = os.environ.get("SHELL", "/bin/bash")
            subprocess.call([shell], cwd=instance_dir)
    except OSError as exc:
        print(f"[ERROR][E10003] 서버 쉘 실행 실패: {exc}")


def _print_instance_summary(instances: list[dict[str, Any]]) -> None:
    if not instances:
        print("- 생성된 인스턴스가 없습니다.")
        return
    for idx, item in enumerate(instances, start=1):
        print(
            f"{idx}. id={item.get('id')} | game={item.get('gameId')} | "
            f"mode={item.get('mode')} | version={item.get('gameVersion', 'n/a')}"
        )


def run_menu(engine: InstallerEngine) -> int:
    print_banner()

    _check_installer_update()
    _check_sdk_dependencies()

    games = engine.list_games()
    game = _step_select_game(games)

    mode, game_version, dependency_versions = _resolve_mode_and_versions(game)

    install_dir, base_dir, path_is_full = _step_select_path()
    server_folder_name, refined_install_dir = _step_server_folder(path_is_full, install_dir)
    if refined_install_dir:
        install_dir = refined_install_dir

    server_name = input(f"서버 표시 이름 (기본: {server_folder_name}): ").strip() or server_folder_name

    ports_raw = input('포트 오버라이드 JSON (예: {"game":25570}) [엔터=기본값]: ').strip()
    ports = parse_ports(ports_raw) if ports_raw else {}

    auto_eula = _step_eula(game["id"])
    if not auto_eula:
        return 1

    print_step(7, "설치")
    print("- 설정 요약")
    print(f"  game: {game['id']}")
    print(f"  mode: {mode}")
    print(f"  server_name: {server_name}")
    print(f"  game_version: {game_version}")
    print(f"  dependency_versions: {json.dumps(dependency_versions, ensure_ascii=False)}")
    print(f"  install_dir: {install_dir or '(none)'}")
    print(f"  base_dir: {base_dir or '(none)'}")
    print(f"  server_folder_name: {server_folder_name}")
    print(f"  ports: {json.dumps(ports, ensure_ascii=False)}")

    if not prompt_yes_no("이 설정으로 설치를 진행할까요?", True):
        print("설치를 취소했습니다.")
        return 0

    job_id = engine.submit_install(
        game_id=game["id"],
        mode=mode,
        name=server_name,
        port_overrides=ports,
        auto_eula=auto_eula,
        game_version=game_version,
        dependency_versions=dependency_versions,
        install_dir=install_dir,
        base_dir=base_dir,
        server_folder_name=server_folder_name,
    )
    result = wait_for_job(engine, job_id)
    if result != 0:
        return result

    job = engine.get_job(job_id) or {}
    instance_id = str(job.get("instanceId", "")).strip()
    instances = engine.list_instances()
    instance = next((item for item in instances if item.get("id") == instance_id), None)

    print("\n[설치 확인]")
    _print_instance_summary(instances)

    _step_network_open(instance)
    _step_port_check(instance)
    _step_finish(instance)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Game Server Installer CLI")
    parser.add_argument(
        "--manifest-dir",
        default=str(Path(__file__).resolve().parents[1] / "manifests" / "games"),
        help="게임 매니페스트 디렉터리",
    )
    parser.add_argument("--data-root", default="~/.gsi", help="상태/인스턴스 루트 경로")
    parser.add_argument("--dry-run", action="store_true", help="실제 실행 없이 로그만 출력")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("menu", help="10단계 대화형 설치 메뉴")
    sub.add_parser("catalog", help="지원 게임 목록")
    sub.add_parser("instances", help="생성된 인스턴스 목록")

    install = sub.add_parser("install", help="새 인스턴스 설치")
    install.add_argument("--game", required=True)
    install.add_argument("--name", required=True)
    install.add_argument("--mode", default="")
    install.add_argument(
        "--ports",
        default="",
        help='JSON object, 예: {"game":25570,"query":27016}',
    )
    install.add_argument(
        "--auto-eula",
        action="store_true",
        help="지원 게임(E.g. Minecraft)의 EULA 파일을 자동 생성/동의 처리",
    )
    install.add_argument(
        "--game-version",
        default="",
        help="게임 서버 버전 (예: 1.21.4, latest)",
    )
    install.add_argument(
        "--dep-versions",
        default="",
        help='추가 종속성 버전 JSON (예: {"java":"21","steamcmd":"latest"})',
    )
    install.add_argument(
        "--install-dir",
        default="",
        help="설치 전체 절대 경로 (예: /srv/game/my-server)",
    )
    install.add_argument(
        "--base-dir",
        default="",
        help="설치 위치 절대 경로 (예: /srv/game). --server-folder와 함께 사용",
    )
    install.add_argument(
        "--server-folder",
        default="",
        help="서버 폴더 이름 (예: my-server)",
    )

    for action in ["start", "stop", "update", "backup", "restore"]:
        cmd = sub.add_parser(action, help=f"인스턴스 {action}")
        cmd.add_argument("--instance", required=True)
        if action == "restore":
            cmd.add_argument("--backup-file", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        ensure_admin_privileges()
    except EngineError as exc:
        print(f"[ERROR] {exc}")
        return 1

    engine = InstallerEngine(
        manifest_dir=Path(args.manifest_dir),
        data_root=args.data_root,
        dry_run=args.dry_run,
    )

    try:
        if args.command == "catalog":
            print(json.dumps(engine.list_games(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "instances":
            print(json.dumps(engine.list_instances(), ensure_ascii=False, indent=2))
            return 0

        if args.command == "menu":
            return run_menu(engine)

        if args.command == "install":
            ports = parse_ports(args.ports) if args.ports else {}
            dep_versions = parse_dep_versions(args.dep_versions) if args.dep_versions else {}
            job_id = engine.submit_install(
                game_id=args.game,
                mode=args.mode or None,
                name=args.name,
                port_overrides=ports,
                auto_eula=bool(args.auto_eula),
                game_version=args.game_version or None,
                dependency_versions=dep_versions,
                install_dir=args.install_dir or None,
                base_dir=args.base_dir or None,
                server_folder_name=args.server_folder or None,
            )
            return wait_for_job(engine, job_id)

        if args.command in {"start", "stop", "update", "backup", "restore"}:
            options: dict[str, Any] = {}
            if args.command == "restore":
                options["backupFile"] = args.backup_file
            job_id = engine.submit_instance_action(args.instance, args.command, options)
            return wait_for_job(engine, job_id)

        parser.print_help()
        return 2
    except (EngineError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    finally:
        engine.shutdown()


if __name__ == "__main__":
    sys.exit(main())
