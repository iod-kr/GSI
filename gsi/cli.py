from __future__ import annotations

import argparse
import json
import locale
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

GITHUB_REPO_SLUG = "iod-kr/GSI"
GITHUB_RELEASE_LATEST_API = f"https://api.github.com/repos/{GITHUB_REPO_SLUG}/releases/latest"

EULA_GUIDE: dict[str, dict[str, str]] = {
    "minecraft": {
        "ko": "Minecraft EULA 동의가 필요합니다. 자동 동의 선택 시 eula.txt에 eula=true가 기록됩니다.",
        "en": "Minecraft EULA acceptance is required. With auto-accept, eula.txt will be written with eula=true.",
    },
    "valheim": {
        "ko": "Valheim 서버 운영 시 게임/서비스 약관을 확인하고 동의 후 진행하세요.",
        "en": "For Valheim server hosting, review and accept the game/service terms before continuing.",
    },
    "cs2": {
        "ko": "Counter-Strike 2 서버 운영 정책 및 Steam 약관을 확인한 뒤 진행하세요.",
        "en": "For Counter-Strike 2 hosting, review server policies and Steam terms before continuing.",
    },
    "palworld": {
        "ko": "Palworld 서버 운영 시 Pocketpair 및 플랫폼 약관을 확인하고 동의하세요.",
        "en": "For Palworld hosting, review and accept Pocketpair and platform terms before continuing.",
    },
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
_LANG = "ko"
_LANG_SOURCE = "default"


class MenuExitRequested(Exception):
    pass


class MenuUninstallRequested(Exception):
    pass


def _tr(ko: str, en: str) -> str:
    return ko if _LANG == "ko" else en


def _set_language(lang: str, source: str) -> None:
    global _LANG, _LANG_SOURCE
    _LANG = "ko" if lang not in {"ko", "en"} else lang
    _LANG_SOURCE = source or "default"


def _normalize_language_code(raw: str | None) -> str | None:
    if not raw:
        return None
    code = str(raw).strip().lower()
    if not code or code == "auto":
        return None
    code = code.split(".", 1)[0].replace("-", "_")
    primary = code.split("_", 1)[0]
    if primary.startswith("ko"):
        return "ko"
    if primary.startswith("en"):
        return "en"
    return None


def _detect_windows_ui_language() -> str | None:
    if os.name != "nt":
        return None
    try:
        import ctypes

        win_locale = locale.windows_locale
        lang_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        return _normalize_language_code(win_locale.get(lang_id))
    except (AttributeError, OSError, ValueError):
        return None


def resolve_language(preferred: str) -> tuple[str, str]:
    normalized_preferred = _normalize_language_code(preferred)
    if preferred != "auto":
        if not normalized_preferred:
            raise EngineError(
                "지원되지 않는 언어 값입니다. --lang auto|ko|en 중 하나를 사용하세요."
            )
        return normalized_preferred, "cli"

    env_override = _normalize_language_code(os.environ.get("GSI_LANG"))
    if env_override:
        return env_override, "env:GSI_LANG"

    for env_name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        detected = _normalize_language_code(os.environ.get(env_name))
        if detected:
            return detected, f"env:{env_name}"

    try:
        sys_locale = locale.getlocale()[0]
    except (ValueError, TypeError):
        sys_locale = None
    detected = _normalize_language_code(sys_locale)
    if detected:
        return detected, "locale.getlocale"

    try:
        default_locale = locale.getdefaultlocale()[0]
    except (ValueError, TypeError):
        default_locale = None
    detected = _normalize_language_code(default_locale)
    if detected:
        return detected, "locale.getdefaultlocale"

    windows_lang = _detect_windows_ui_language()
    if windows_lang:
        return windows_lang, "windows-ui"

    return "ko", "default"


def parse_ports(raw: str) -> dict[str, int]:
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(_tr("ports는 JSON object여야 합니다.", "ports must be a JSON object."))
    return {str(k): int(v) for k, v in payload.items()}


def parse_dep_versions(raw: str) -> dict[str, str]:
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(_tr("dep-versions는 JSON object여야 합니다.", "dep-versions must be a JSON object."))
    return {str(k): str(v) for k, v in payload.items()}


def print_banner() -> None:
    print("=" * 72)
    print(" GSI Installer CLI")
    print(_tr(" 대화형 게임 서버 설치기 (Windows CMD / Linux Terminal)", " Interactive Game Server Installer (Windows CMD / Linux Terminal)"))
    print(
        _tr(
            f" 언어: {_LANG} ({_LANG_SOURCE})",
            f" Language: {_LANG} ({_LANG_SOURCE})",
        )
    )
    print("=" * 72)


def print_step(number: int, title: str) -> None:
    _ = number
    print(f"\n[{title}]")


def _enable_windows_ansi() -> bool:
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return False
        # ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
        if kernel32.SetConsoleMode(handle, mode.value | 0x0004) == 0:
            return False
        return True
    except (AttributeError, OSError):
        return False


def _can_use_arrow_menu() -> bool:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    return _enable_windows_ansi()


def _read_menu_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in {"\r", "\n"}:
            return "enter"
        if ch in {"\x00", "\xe0"}:
            code = msvcrt.getwch()
            mapping = {"H": "up", "P": "down", "K": "left", "M": "right"}
            return mapping.get(code, "unknown")
        if ch == "\x1b":
            return "esc"
        return ch.lower()

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in {"\r", "\n"}:
            return "enter"
        if ch == "\x1b":
            next_char = sys.stdin.read(1)
            if next_char == "[":
                code = sys.stdin.read(1)
                mapping = {"A": "up", "B": "down", "C": "right", "D": "left"}
                return mapping.get(code, "unknown")
            return "esc"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _render_arrow_menu(prompt: str, options: list[str], selected_index: int, prev_lines: int = 0) -> int:
    if prev_lines > 0:
        sys.stdout.write(f"\x1b[{prev_lines}F")

    lines: list[str] = [
        prompt,
        _tr("  ↑/↓ 로 이동, Enter 로 선택", "  Use ↑/↓ to move, Enter to select"),
    ]
    for idx, option in enumerate(options):
        pointer = "▶" if idx == selected_index else " "
        lines.append(f" {pointer} {option}")

    for line in lines:
        sys.stdout.write(f"\x1b[2K{line}\n")
    sys.stdout.flush()
    return len(lines)


def _choose_index_arrow(prompt: str, options: list[str], default_index: int = 0) -> int:
    selected = max(0, min(default_index, len(options) - 1))
    rendered_lines = _render_arrow_menu(prompt, options, selected)
    while True:
        key = _read_menu_key()
        if key in {"up", "k"}:
            selected = (selected - 1) % len(options)
            rendered_lines = _render_arrow_menu(prompt, options, selected, rendered_lines)
            continue
        if key in {"down", "j"}:
            selected = (selected + 1) % len(options)
            rendered_lines = _render_arrow_menu(prompt, options, selected, rendered_lines)
            continue
        if key in {"enter", "right"}:
            return selected
        if key in {"esc", "left"}:
            return default_index


def _choose_index_numeric(
    prompt: str,
    total: int,
    default_index: int = 0,
    options: list[str] | None = None,
    allow_control_actions: bool = True,
) -> int:
    if options:
        for idx, label in enumerate(options, start=1):
            print(f"{idx}. {label}")
    if allow_control_actions:
        print(_tr("0. EXIT", "0. EXIT"))
        print(_tr("-1. Uninstall", "-1. Uninstall"))

    default_number = default_index + 1
    while True:
        raw = input(
            _tr(
                f"{prompt} [기본 {default_number}]: ",
                f"{prompt} [default {default_number}]: ",
            )
        ).strip()
        if not raw:
            return default_index
        if allow_control_actions and raw == "0":
            raise MenuExitRequested
        if allow_control_actions and raw == "-1":
            raise MenuUninstallRequested
        if raw.isdigit():
            number = int(raw)
            if 1 <= number <= total:
                return number - 1
        print(_tr(f"1~{total} 범위의 숫자를 입력하세요.", f"Enter a number between 1 and {total}."))


def choose_index(
    prompt: str,
    total: int,
    default_index: int = 0,
    options: list[str] | None = None,
    allow_control_actions: bool = True,
) -> int:
    if total <= 0:
        raise ValueError(_tr("선택 가능한 항목이 없습니다.", "No selectable items are available."))
    if options is not None and len(options) != total:
        raise ValueError(_tr("옵션 개수가 올바르지 않습니다.", "Invalid option count."))
    if options and _can_use_arrow_menu():
        menu_options = [str(item) for item in options]
        exit_idx: int | None = None
        uninstall_idx: int | None = None
        if allow_control_actions:
            menu_options.append(_tr("EXIT", "EXIT"))
            exit_idx = len(menu_options) - 1
            menu_options.append(_tr("Uninstall", "Uninstall"))
            uninstall_idx = len(menu_options) - 1

        selected = _choose_index_arrow(prompt, menu_options, default_index)
        if allow_control_actions and selected == exit_idx:
            raise MenuExitRequested
        if allow_control_actions and selected == uninstall_idx:
            raise MenuUninstallRequested
        return selected

    return _choose_index_numeric(
        prompt,
        total,
        default_index,
        options,
        allow_control_actions=allow_control_actions,
    )


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{prompt} [{default_text}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "ㅛ"}:
            return True
        if raw in {"n", "no", "ㅜ"}:
            return False
        print(_tr("y 또는 n으로 입력하세요.", "Enter y or n."))


def ensure_admin_privileges() -> None:
    if os.name == "nt":
        ok, _ = _run_command(["net", "session"])
        if not ok:
            ok, _ = _run_command(["fltmc"])
        if not ok:
            raise EngineError(
                _tr(
                    "[E0001] 관리자 권한이 필요합니다. CMD/PowerShell을 '관리자 권한으로 실행' 후 다시 시도하세요.",
                    "[E0001] Administrator privileges are required. Re-run CMD/PowerShell as Administrator.",
                )
            )
        return

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise EngineError(
            _tr(
                "[E0001] 관리자 권한(root)이 필요합니다. sudo 또는 root 쉘에서 다시 실행하세요.",
                "[E0001] Root privileges are required. Re-run with sudo or from a root shell.",
            )
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
                _tr(
                    f"[E2103] {dep_name}: 자동 설치를 위한 패키지 매니저(또는 레시피)를 찾지 못했습니다.",
                    f"[E2103] {dep_name}: no package manager/recipe found for auto-install.",
                ),
            )
        return (
            False,
            _tr(
                f"[E2103] {dep_name}: {manager} 환경에서 자동 설치 레시피가 없습니다.",
                f"[E2103] {dep_name}: no auto-install recipe for {manager}.",
            ),
        )

    for cmd in commands:
        rendered = " ".join(cmd)
        print(_tr(f"- 실행: {rendered}", f"- Run: {rendered}"))
        ok, output = _run_with_optional_sudo(cmd) if os.name != "nt" else _run_command(cmd)
        if not ok:
            detail = output.strip() or "no output"
            return (
                False,
                _tr(
                    f"[E2102] {dep_name} 자동 설치 실패 ({manager})\\n{detail}",
                    f"[E2102] {dep_name} auto-install failed ({manager})\\n{detail}",
                ),
            )
        if cmd[:2] == ["apt-get", "update"]:
            _APT_UPDATED = True
    return (
        True,
        _tr(
            f"[OK] {dep_name} 자동 설치 성공 ({manager})",
            f"[OK] {dep_name} auto-install succeeded ({manager})",
        ),
    )


def wait_for_job(engine: InstallerEngine, job_id: str, poll_sec: float = 0.8) -> int:
    last_log = ""
    while True:
        job = engine.get_job(job_id)
        if not job:
            print(_tr(f"[ERROR][E7002] job not found: {job_id}", f"[ERROR][E7002] job not found: {job_id}"))
            return 2

        log = job.get("log", "")
        if log != last_log:
            print(log[len(last_log) :], end="")
            last_log = log

        status = job.get("status")
        if status in {"succeeded", "failed"}:
            if status == "succeeded":
                print(_tr(f"\n[OK] 작업 완료: {job_id}", f"\n[OK] job {job_id} completed"))
                return 0
            print(_tr(f"\n[FAILED] {job.get('error', 'unknown error')}", f"\n[FAILED] {job.get('error', 'unknown error')}"))
            return 1

        time.sleep(poll_sec)


def _normalize_release_version(raw: str) -> str:
    value = str(raw).strip()
    if value.lower().startswith("v"):
        return value[1:]
    return value


def _check_installer_update() -> None:
    print_step(1, _tr("설치기 업데이트 확인", "Installer Update Check"))
    print(_tr(f"- 현재 설치기 버전: {__version__}", f"- Current installer version: {__version__}"))
    try:
        request = urllib.request.Request(
            GITHUB_RELEASE_LATEST_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "gsi-installer-cli",
            },
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read().decode("utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            print(_tr("[WARN][E1002] 업데이트 응답 형식이 올바르지 않습니다.", "[WARN][E1002] Invalid update response format."))
            return

        latest_tag = str(payload.get("tag_name", "")).strip()
        latest = _normalize_release_version(latest_tag)
        if not latest or not latest_tag:
            print(
                _tr(
                    "[WARN][E1003] 최신 릴리스 tag_name 정보가 비어 있습니다.",
                    "[WARN][E1003] Latest release tag_name is empty.",
                )
            )
            return

        if latest == __version__:
            print(
                _tr(
                    f"[OK] 최신 버전입니다. (현재={__version__}, 릴리스={latest_tag})",
                    f"[OK] You are on the latest version. (current={__version__}, release={latest_tag})",
                )
            )
        else:
            release_name = str(payload.get("name", "")).strip()
            release_url = str(payload.get("html_url", "")).strip()
            published_at = str(payload.get("published_at", "")).strip()
            print(
                _tr(
                    f"[WARN][E1004] 업데이트 가능: 현재={__version__}, 최신={latest_tag}",
                    f"[WARN][E1004] Update available: current={__version__}, latest={latest_tag}",
                )
            )
            if release_name:
                print(_tr(f"- 릴리스 이름: {release_name}", f"- Release name: {release_name}"))
            if published_at:
                print(_tr(f"- 배포 시각: {published_at}", f"- Published at: {published_at}"))
            if release_url:
                print(_tr(f"- 릴리스 URL: {release_url}", f"- Release URL: {release_url}"))
    except urllib.error.URLError as exc:
        print(_tr(f"[WARN][E1001] 업데이트 확인 실패(네트워크/주소): {exc}", f"[WARN][E1001] Update check failed (network/url): {exc}"))
        text = str(exc).lower()
        if "404" in text:
            print(
                _tr(
                    "[WARN][E1006] GitHub 최신 릴리스를 찾지 못했습니다. (릴리스 미게시/권한 문제 가능)",
                    "[WARN][E1006] Latest GitHub release not found. (no release yet / permission issue)",
                )
            )
        if "403" in text or "rate limit" in text:
            print(
                _tr(
                    "[WARN][E1007] GitHub API 요청 제한 또는 권한 문제일 수 있습니다.",
                    "[WARN][E1007] GitHub API may be rate-limited or access-restricted.",
                )
            )
    except json.JSONDecodeError as exc:
        print(_tr(f"[WARN][E1005] 업데이트 응답 JSON 파싱 실패: {exc}", f"[WARN][E1005] Failed to parse update JSON: {exc}"))


def _check_sdk_dependencies() -> None:
    print_step(2, _tr("필요 SDK/의존성 확인", "SDK/Dependency Check"))
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
                print(_tr(f"[ERROR][E2001] 필수 의존성 누락: {name}", f"[ERROR][E2001] Missing required dependency: {name}"))
                missing_required.append((name, candidates))
            else:
                print(
                    _tr(
                        f"[WARN][E2002] 선택 의존성 누락: {name} (선택한 게임/모드에서 필요 시 설치 실패 가능)",
                        f"[WARN][E2002] Missing optional dependency: {name} (install may fail if required by chosen game/mode)",
                    )
                )
                missing_optional.append((name, candidates))

    if not missing_required and not missing_optional:
        print(_tr("[OK] 의존성 점검 완료: 누락 항목 없음", "[OK] Dependency check complete: nothing missing"))
        return

    missing_names = [name for name, _ in [*missing_required, *missing_optional]]
    print(_tr(f"- 누락 의존성: {', '.join(missing_names)}", f"- Missing dependencies: {', '.join(missing_names)}"))

    default_auto_install = bool(missing_required)
    if not prompt_yes_no(
        _tr("누락 의존성 자동 설치를 시도할까요?", "Try automatic install for missing dependencies?"),
        default_auto_install,
    ):
        if missing_required:
            raise EngineError(
                _tr(
                    "[E2003] 필수 의존성 누락 상태로 설치를 계속할 수 없습니다.",
                    "[E2003] Cannot continue due to missing required dependencies.",
                )
            )
        print(_tr("[INFO] 선택 의존성 자동 설치를 건너뜁니다.", "[INFO] Skipping optional dependency auto-install."))
        return

    for name, _ in [*missing_required, *missing_optional]:
        required = any(req_name == name for req_name, _ in missing_required)
        default_choice = True if required else False
        if not prompt_yes_no(
            _tr(f"{name} 자동 설치를 진행할까요?", f"Install {name} automatically?"),
            default_choice,
        ):
            if required:
                raise EngineError(
                    _tr(
                        f"[E2004] 필수 의존성({name}) 자동 설치 거부로 설치를 중단합니다.",
                        f"[E2004] Aborting: required dependency auto-install declined ({name}).",
                    )
                )
            print(_tr(f"[INFO] 선택 의존성({name}) 자동 설치를 건너뜁니다.", f"[INFO] Skipping optional dependency auto-install ({name})."))
            continue

        ok, detail = _attempt_dependency_install(name)
        if ok:
            print(detail)
        else:
            print(f"[ERROR] {detail}")
            if required:
                raise EngineError(
                    _tr(
                        f"[E2005] 필수 의존성({name}) 자동 설치 실패로 설치를 중단합니다.",
                        f"[E2005] Aborting: required dependency auto-install failed ({name}).",
                    )
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
            _tr(
                f"[E2006] 필수 의존성 재검증 실패: {', '.join(unresolved_required)}",
                f"[E2006] Required dependency re-check failed: {', '.join(unresolved_required)}",
            )
        )

    if unresolved_optional:
        print(
            _tr(
                f"[WARN][E2007] 선택 의존성 미설치: {', '.join(unresolved_optional)} (선택한 게임/모드에 따라 이후 실패 가능)",
                f"[WARN][E2007] Optional dependencies still missing: {', '.join(unresolved_optional)} (later steps may fail based on game/mode)",
            )
        )
    else:
        print(_tr("[OK] 누락 의존성 자동 설치/재검증 완료", "[OK] Missing dependency auto-install/re-check complete"))


def _step_select_game(games: list[dict[str, Any]]) -> dict[str, Any]:
    print_step(3, _tr("게임 목록", "Game List"))
    game_names = [str(game["name"]) for game in games]
    selected = games[
        choose_index(
            _tr("설치할 게임 선택", "Select a game to install"),
            len(games),
            0,
            game_names,
        )
    ]
    print(_tr(f"- 선택됨: {selected['name']}", f"- Selected: {selected['name']}"))
    return selected


def _step_select_path() -> tuple[str | None, str | None, bool]:
    print_step(4, _tr("경로 지정", "Path Selection"))
    path_options = [
        _tr("전체 풀 경로 직접 입력", "Enter full absolute path"),
        _tr("위치 경로만 입력", "Enter base path only"),
    ]
    choice = choose_index(
        _tr("경로 방식 선택", "Select path mode"),
        len(path_options),
        0,
        path_options,
    )

    if choice == 0:
        install_dir = input(_tr("설치 전체 경로(절대 경로) 입력: ", "Enter full install path (absolute): ")).strip()
        if not install_dir:
            raise EngineError(_tr("[E4001] 설치 전체 경로가 비어 있습니다.", "[E4001] Full install path is empty."))
        if not Path(install_dir).expanduser().is_absolute():
            raise EngineError(_tr("[E4002] 설치 전체 경로는 절대 경로여야 합니다.", "[E4002] Full install path must be absolute."))
        return str(Path(install_dir).expanduser()), None, True

    base_dir = input(_tr("설치 위치 경로(절대 경로) 입력: ", "Enter install base path (absolute): ")).strip()
    if not base_dir:
        raise EngineError(_tr("[E4003] 설치 위치 경로가 비어 있습니다.", "[E4003] Install base path is empty."))
    if not Path(base_dir).expanduser().is_absolute():
        raise EngineError(_tr("[E4004] 설치 위치 경로는 절대 경로여야 합니다.", "[E4004] Install base path must be absolute."))
    return None, str(Path(base_dir).expanduser()), False


def _step_server_folder(path_is_full: bool, install_dir: str | None) -> tuple[str, str | None]:
    print_step(5, _tr("서버 폴더 이름", "Server Folder Name"))

    if path_is_full:
        full_path = Path(str(install_dir)).expanduser()
        default_name = full_path.name or "server"
        folder_name = (
            input(
                _tr(
                    f"서버 폴더 이름(기본: {default_name}): ",
                    f"Server folder name (default: {default_name}): ",
                )
            ).strip()
            or default_name
        )
        final_path = str(full_path.parent / folder_name)
        print(_tr(f"- 최종 설치 경로: {final_path}", f"- Final install path: {final_path}"))
        return folder_name, final_path

    folder_name = input(
        _tr(
            "서버 폴더 이름 입력 (예: my-game-server): ",
            "Enter server folder name (e.g. my-game-server): ",
        )
    ).strip()
    if not folder_name:
        raise EngineError(_tr("[E5001] 서버 폴더 이름이 비어 있습니다.", "[E5001] Server folder name is empty."))
    return folder_name, None


def _resolve_mode_and_versions(game: dict[str, Any]) -> tuple[str, str, dict[str, str]]:
    modes = list(game["modes"])
    default_mode = str(game.get("defaultMode", modes[0]))
    default_mode_index = modes.index(default_mode) if default_mode in modes else 0
    mode = modes[
        choose_index(
            _tr("모드 선택", "Select mode"),
            len(modes),
            default_mode_index,
            [str(mode_name) for mode_name in modes],
        )
    ]

    version_opts = game.get("versionOptions", {}) if isinstance(game.get("versionOptions", {}), dict) else {}
    default_game_version = str(version_opts.get("default", "latest")) or "latest"
    choices = version_opts.get("choices", [])
    if isinstance(choices, list) and choices:
        default_ver_index = choices.index(default_game_version) if default_game_version in choices else 0
        game_version = str(
            choices[
                choose_index(
                    _tr("게임 버전 선택", "Select game version"),
                    len(choices),
                    default_ver_index,
                    [str(item) for item in choices],
                )
            ]
        )
    else:
        game_version = (
            input(
                _tr(
                    f"게임 버전 (기본: {default_game_version}): ",
                    f"Game version (default: {default_game_version}): ",
                )
            ).strip()
            or default_game_version
        )

    dependency_versions: dict[str, str] = {}
    dep_opts = game.get("dependencyOptions", {}) if isinstance(game.get("dependencyOptions", {}), dict) else {}
    if dep_opts:
        for dep_name, dep_cfg in dep_opts.items():
            if not isinstance(dep_cfg, dict):
                continue
            dep_default = str(dep_cfg.get("default", "")).strip()
            dep_choices = dep_cfg.get("choices", [])
            dep_value = dep_default
            if isinstance(dep_choices, list) and dep_choices:
                default_dep_index = dep_choices.index(dep_default) if dep_default in dep_choices else 0
                dep_value = str(
                    dep_choices[
                        choose_index(
                            _tr(f"{dep_name} 버전 선택", f"Select {dep_name} version"),
                            len(dep_choices),
                            default_dep_index,
                            [str(item) for item in dep_choices],
                        )
                    ]
                )
            else:
                dep_value = (
                    input(
                        _tr(
                            f"  {dep_name} 버전 (기본: {dep_default or '없음'}): ",
                            f"  {dep_name} version (default: {dep_default or 'none'}): ",
                        )
                    ).strip()
                    or dep_default
                )
            if dep_value:
                dependency_versions[dep_name] = dep_value

    return mode, game_version, dependency_versions


def _step_eula(game_id: str) -> bool:
    print_step(6, _tr("EULA 동의", "EULA Agreement"))
    guide = (
        EULA_GUIDE.get(game_id, {}).get(
            _LANG,
            _tr(
                "게임/서비스 약관을 확인하고 동의 후 진행하세요.",
                "Review and accept the game/service terms before continuing.",
            ),
        )
    )
    print(guide)
    agreed = prompt_yes_no(
        _tr(
            "위 내용을 확인했고 자동 동의 옵션으로 진행하시겠습니까?",
            "I reviewed the terms. Continue with automatic EULA agreement?",
        ),
        False,
    )
    if not agreed:
        print(_tr("[INFO] EULA 미동의로 설치를 중단합니다.", "[INFO] Installation aborted: EULA not accepted."))
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
    print_step(8, _tr("네트워크 개방 작업", "Network Opening"))
    if not instance:
        print(
            _tr(
                "[WARN][E8000] 설치 결과 인스턴스를 찾지 못해 네트워크 개방 단계를 건너뜁니다.",
                "[WARN][E8000] Install result instance not found; skipping network opening step.",
            )
        )
        return

    ports = instance.get("ports", {}) if isinstance(instance.get("ports", {}), dict) else {}
    port_list = sorted({int(v) for v in ports.values()})
    if not port_list:
        print(_tr("[WARN][E8004] 개방할 포트가 없습니다.", "[WARN][E8004] No ports to open."))
        return

    print(_tr(f"- 대상 포트: {port_list}", f"- Target ports: {port_list}"))
    if not prompt_yes_no(_tr("자동으로 포트 개방 작업을 시도할까요?", "Try opening ports automatically?"), True):
        print(_tr("[INFO] 사용자가 포트 개방 자동 작업을 건너뛰었습니다.", "[INFO] User skipped automatic port opening."))
        return

    if os.name == "nt":
        if not shutil.which("netsh"):
            print(_tr("[ERROR][E8001] netsh를 찾을 수 없어 포트 개방을 진행할 수 없습니다.", "[ERROR][E8001] netsh not found; cannot open ports."))
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
                    print(_tr(f"[OK] 방화벽 규칙 추가: {rule_name}", f"[OK] Firewall rule added: {rule_name}"))
                else:
                    print(_tr(f"[ERROR][E8002] 방화벽 규칙 추가 실패: {rule_name}", f"[ERROR][E8002] Failed to add firewall rule: {rule_name}"))
                    if output:
                        print(output)
        print(_tr("[INFO] Windows 방화벽 개방 작업이 끝났습니다.", "[INFO] Windows firewall open step finished."))
        return

    if shutil.which("ufw"):
        for port in port_list:
            for proto in ("tcp", "udp"):
                ok, output = _run_with_optional_sudo(["ufw", "allow", f"{port}/{proto}"])
                if ok:
                    print(f"[OK] ufw allow {port}/{proto}")
                else:
                    print(_tr(f"[ERROR][E8002] ufw 규칙 추가 실패: {port}/{proto}", f"[ERROR][E8002] Failed to add ufw rule: {port}/{proto}"))
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
                    print(_tr(f"[ERROR][E8002] firewall-cmd 규칙 추가 실패: {port}/{proto}", f"[ERROR][E8002] Failed to add firewall-cmd rule: {port}/{proto}"))
                    if output:
                        print(output)
        ok, output = _run_with_optional_sudo(["firewall-cmd", "--reload"])
        if ok:
            print("[OK] firewall-cmd reload")
        else:
            print(_tr("[ERROR][E8003] firewall-cmd reload 실패", "[ERROR][E8003] firewall-cmd reload failed"))
            if output:
                print(output)
        return

    print(
        _tr(
            "[ERROR][E8001] 지원되는 방화벽 도구(ufw/firewall-cmd/netsh)를 찾을 수 없습니다.",
            "[ERROR][E8001] Supported firewall tool not found (ufw/firewall-cmd/netsh).",
        )
    )


def _check_local_tcp_open(port: int, timeout_sec: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _step_port_check(instance: dict[str, Any] | None) -> None:
    print_step(9, _tr("외부 접속 테스트/포트 체크", "External Access Test / Port Check"))
    if not instance:
        print(
            _tr(
                "[WARN][E9000] 설치 결과 인스턴스를 찾지 못해 포트 체크를 건너뜁니다.",
                "[WARN][E9000] Install result instance not found; skipping port check.",
            )
        )
        return

    ports = instance.get("ports", {}) if isinstance(instance.get("ports", {}), dict) else {}
    port_list = sorted({int(v) for v in ports.values()})
    if not port_list:
        print(_tr("[WARN][E9001] 확인할 포트가 없습니다.", "[WARN][E9001] No ports to check."))
        return

    for port in port_list:
        status = "OPEN" if _check_local_tcp_open(port) else "CLOSED/NOT_LISTENING"
        print(f"- localhost:{port} -> {status}")

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as response:
            public_ip = response.read().decode("utf-8").strip()
        if public_ip:
            preview = ", ".join(str(p) for p in port_list)
            print(
                _tr(
                    f"- 외부 접속 확인용 공인 IP: {public_ip} (포트: {preview})",
                    f"- Public IP for external access checks: {public_ip} (ports: {preview})",
                )
            )
    except urllib.error.URLError as exc:
        print(_tr(f"[WARN][E9002] 공인 IP 조회 실패: {exc}", f"[WARN][E9002] Failed to query public IP: {exc}"))


def _notify_windows_ready(message: str) -> None:
    if shutil.which("msg"):
        _run_command(["msg", "*", message])
        return
    if shutil.which("powershell"):
        _run_command(["powershell", "-NoProfile", "-Command", f"Write-Host '{message}'"])


def _step_finish(instance: dict[str, Any] | None) -> None:
    print_step(10, _tr("서버 오픈 알림/서버 쉘", "Server Ready Notification / Server Shell"))
    if not instance:
        print(
            _tr(
                "[WARN][E10001] 설치 결과 인스턴스를 찾지 못해 최종 단계를 축약합니다.",
                "[WARN][E10001] Install result instance not found; shortening final step.",
            )
        )
        return

    msg = _tr(
        f"서버 준비 완료: id={instance.get('id')} game={instance.get('gameId')} mode={instance.get('mode')}",
        f"Server ready: id={instance.get('id')} game={instance.get('gameId')} mode={instance.get('mode')}",
    )
    print(f"[OK] {msg}")

    if os.name == "nt":
        _notify_windows_ready(f"GSI: {msg}")

    if not prompt_yes_no(_tr("서버 폴더 쉘을 지금 열까요?", "Open a shell in the server folder now?"), False):
        return

    instance_dir = str(instance.get("instanceDir", "")).strip()
    if not instance_dir:
        print(_tr("[WARN][E10002] instanceDir가 없어 쉘을 열 수 없습니다.", "[WARN][E10002] Cannot open shell because instanceDir is missing."))
        return

    try:
        if os.name == "nt":
            subprocess.call(["cmd", "/K"], cwd=instance_dir)
        else:
            shell = os.environ.get("SHELL", "/bin/bash")
            subprocess.call([shell], cwd=instance_dir)
    except OSError as exc:
        print(_tr(f"[ERROR][E10003] 서버 쉘 실행 실패: {exc}", f"[ERROR][E10003] Failed to launch server shell: {exc}"))


def _print_instance_summary(instances: list[dict[str, Any]]) -> None:
    if not instances:
        print(_tr("- 생성된 인스턴스가 없습니다.", "- No instances were created."))
        return
    for idx, item in enumerate(instances, start=1):
        print(
            f"{idx}. id={item.get('id')} | game={item.get('gameId')} | "
            f"mode={item.get('mode')} | version={item.get('gameVersion', 'n/a')}"
        )


def _menu_uninstall(engine: InstallerEngine) -> int:
    print_step(0, _tr("Uninstall", "Uninstall"))
    print(
        _tr(
            "[WARN] GSI 설치기/데이터 경로를 제거합니다. (복구 불가)",
            "[WARN] This removes GSI installer/data paths. (irreversible)",
        )
    )
    if not prompt_yes_no(
        _tr("정말 제거할까요?", "Proceed with uninstall?"),
        False,
    ):
        print(_tr("[INFO] 제거를 취소했습니다.", "[INFO] Uninstall canceled."))
        return 0

    raw_targets: list[Path] = []
    data_root = Path(str(engine.data_root)).expanduser()
    raw_targets.append(data_root)

    if os.name == "nt":
        program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        raw_targets.append(program_data / "GSI")
    else:
        raw_targets.extend(
            [
                Path("/usr/local/bin/gsi"),
                Path("/opt/gsi"),
                Path("/var/log/gsi"),
                Path("/var/run/gsi"),
            ]
        )

    targets: list[Path] = []
    seen: set[str] = set()
    for item in raw_targets:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        targets.append(item)

    failures: list[str] = []
    for target in targets:
        try:
            if not target.exists() and not target.is_symlink():
                print(_tr(f"- 건너뜀(없음): {target}", f"- Skip (not found): {target}"))
                continue
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            print(_tr(f"[OK] 제거 완료: {target}", f"[OK] Removed: {target}"))
        except OSError as exc:
            failures.append(f"{target}: {exc}")
            print(_tr(f"[ERROR] 제거 실패: {target} ({exc})", f"[ERROR] Remove failed: {target} ({exc})"))

    if failures:
        print(_tr("[ERROR] 일부 경로 제거에 실패했습니다.", "[ERROR] Failed to remove some paths."))
        return 1
    print(_tr("[OK] Uninstall 완료", "[OK] Uninstall completed"))
    return 0


def run_menu(engine: InstallerEngine) -> int:
    print_banner()
    try:
        _check_installer_update()
        _check_sdk_dependencies()

        games = engine.list_games()
        game = _step_select_game(games)

        mode, game_version, dependency_versions = _resolve_mode_and_versions(game)

        install_dir, base_dir, path_is_full = _step_select_path()
        server_folder_name, refined_install_dir = _step_server_folder(path_is_full, install_dir)
        if refined_install_dir:
            install_dir = refined_install_dir

        server_name = (
            input(
                _tr(
                    f"서버 표시 이름 (기본: {server_folder_name}): ",
                    f"Server display name (default: {server_folder_name}): ",
                )
            ).strip()
            or server_folder_name
        )

        ports_raw = input(
            _tr(
                '포트 오버라이드 JSON (예: {"game":25570}) [엔터=기본값]: ',
                'Port override JSON (e.g. {"game":25570}) [enter=default]: ',
            )
        ).strip()
        ports = parse_ports(ports_raw) if ports_raw else {}

        auto_eula = _step_eula(game["id"])
        if not auto_eula:
            return 1

        print_step(7, _tr("설치", "Installation"))
        print(_tr("- 설정 요약", "- Configuration Summary"))
        print(f"  game: {game['id']}")
        print(f"  mode: {mode}")
        print(f"  server_name: {server_name}")
        print(f"  game_version: {game_version}")
        print(f"  dependency_versions: {json.dumps(dependency_versions, ensure_ascii=False)}")
        print(f"  install_dir: {install_dir or '(none)'}")
        print(f"  base_dir: {base_dir or '(none)'}")
        print(f"  server_folder_name: {server_folder_name}")
        print(f"  ports: {json.dumps(ports, ensure_ascii=False)}")

        if not prompt_yes_no(_tr("이 설정으로 설치를 진행할까요?", "Proceed with installation using this configuration?"), True):
            print(_tr("설치를 취소했습니다.", "Installation canceled."))
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

        print(_tr("\n[설치 확인]", "\n[Installation Check]"))
        _print_instance_summary(instances)

        _step_network_open(instance)
        _step_port_check(instance)
        _step_finish(instance)
        return 0
    except MenuExitRequested:
        print(_tr("[INFO] 사용자 요청으로 종료합니다.", "[INFO] Exit requested by user."))
        return 0
    except MenuUninstallRequested:
        return _menu_uninstall(engine)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Game Server Installer CLI")
    parser.add_argument(
        "--lang",
        default="auto",
        choices=["auto", "ko", "en"],
        help="출력 언어 선택 (auto/ko/en). 기본값: auto (환경 기반 자동 감지)",
    )
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
        resolved_lang, source = resolve_language(args.lang)
    except EngineError as exc:
        print(f"[ERROR] {exc}")
        return 1
    _set_language(resolved_lang, source)

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
