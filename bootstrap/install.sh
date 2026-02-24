#!/usr/bin/env bash
set -euo pipefail

GSI_REPO_URL="https://github.com/project-gsi/gsi-installer.git"
GSI_REPO_BRANCH="main"
INSTALL_ROOT="/opt/gsi"
APP_ROOT="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/.venv"
DATA_ROOT="/var/lib/gsi"
LOG_ROOT="/var/log/gsi"
RUN_ROOT="/var/run/gsi"
LAUNCHER_PATH="/usr/local/bin/gsi"

SKIP_RUN=0
FORWARD_ARGS=()

if [[ -t 1 ]]; then
  C_RESET="\033[0m"
  C_BLUE="\033[1;34m"
  C_GREEN="\033[1;32m"
  C_YELLOW="\033[1;33m"
  C_RED="\033[1;31m"
else
  C_RESET=""
  C_BLUE=""
  C_GREEN=""
  C_YELLOW=""
  C_RED=""
fi

print_banner() {
  echo -e "${C_BLUE}======================================================================${C_RESET}"
  echo -e "${C_BLUE} GSI Installer (.sh) - GitHub Connected${C_RESET}"
  echo -e "${C_BLUE} Install Root: $INSTALL_ROOT${C_RESET}"
  echo -e "${C_BLUE} Data Root:    $DATA_ROOT${C_RESET}"
  echo -e "${C_BLUE} Repo:         $GSI_REPO_URL${C_RESET}"
  echo -e "${C_BLUE}======================================================================${C_RESET}"
}

step() {
  echo -e "${C_BLUE}[STEP]${C_RESET} $1"
}

ok() {
  echo -e "${C_GREEN}[OK]${C_RESET} $1"
}

warn() {
  echo -e "${C_YELLOW}[WARN]${C_RESET} $1"
}

err() {
  echo -e "${C_RED}[ERROR]${C_RESET} $1"
}

usage() {
  cat <<USAGE
Usage: bash bootstrap/install.sh [options] [-- <gsi args...>]

This script requires administrator(root) privilege.
It installs/updates GSI from fixed GitHub repository and runs it.

Options:
  --skip-run             Install/update only, do not run gsi
  --branch <name>        Git branch (default: main)
  --data-root <path>     GSI data root (default: /var/lib/gsi)
  --help                 Show help

Examples:
  sudo bash bootstrap/install.sh
  sudo bash bootstrap/install.sh --skip-run
  sudo bash bootstrap/install.sh -- catalog
  sudo bash bootstrap/install.sh -- menu
USAGE
}

require_admin() {
  if [[ "$(id -u)" -ne 0 ]]; then
    err "[E0001] 관리자 권한(root)이 필요합니다. sudo로 실행하세요."
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --skip-run)
        SKIP_RUN=1
        shift
        ;;
      --branch)
        GSI_REPO_BRANCH="$2"
        shift 2
        ;;
      --data-root)
        DATA_ROOT="$2"
        shift 2
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      --)
        shift
        FORWARD_ARGS+=("$@")
        break
        ;;
      *)
        FORWARD_ARGS+=("$1")
        shift
        ;;
    esac
  done
}

ensure_installer_dependencies() {
  step "설치기 의존성 확인"
  local missing=0
  for cmd in git python3; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      err "[E3001] 필수 설치기 의존성 누락: $cmd"
      missing=1
    else
      ok "$cmd 확인됨"
    fi
  done

  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi
}

prepare_directories() {
  step "설치 경로 준비"
  mkdir -p "$INSTALL_ROOT" "$DATA_ROOT" "$LOG_ROOT" "$RUN_ROOT"
  ok "경로 준비 완료"
}

sync_github_repo() {
  step "GitHub 저장소 동기화"

  if [[ -d "$APP_ROOT/.git" ]]; then
    git -C "$APP_ROOT" remote set-url origin "$GSI_REPO_URL"
    git -C "$APP_ROOT" fetch --depth 1 origin "$GSI_REPO_BRANCH"
    git -C "$APP_ROOT" checkout -B "$GSI_REPO_BRANCH" "origin/$GSI_REPO_BRANCH"
    ok "저장소 업데이트 완료"
    return
  fi

  if [[ -e "$APP_ROOT" && ! -d "$APP_ROOT/.git" ]]; then
    err "[E3002] $APP_ROOT 가 Git 저장소가 아닙니다. 경로를 정리 후 재시도하세요."
    exit 1
  fi

  git clone --depth 1 --branch "$GSI_REPO_BRANCH" "$GSI_REPO_URL" "$APP_ROOT"
  ok "저장소 클론 완료"
}

setup_python_runtime() {
  step "Python 런타임 설정"
  if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
  fi

  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV_DIR/bin/python" -m pip install -r "$APP_ROOT/requirements.txt" >/dev/null
  ok "Python 런타임 준비 완료"
}

write_launcher() {
  step "실행 런처 설치"
  cat > "$LAUNCHER_PATH" <<LAUNCHER
#!/usr/bin/env bash
set -euo pipefail
exec "$VENV_DIR/bin/python" -m gsi --data-root "$DATA_ROOT" "\$@"
LAUNCHER
  chmod +x "$LAUNCHER_PATH"
  ok "런처 설치 완료: $LAUNCHER_PATH"
}

run_gsi() {
  if [[ "$SKIP_RUN" -eq 1 ]]; then
    warn "--skip-run 옵션으로 실행 단계를 건너뜁니다."
    return
  fi

  step "GSI 실행"
  local args=("${FORWARD_ARGS[@]}")
  if [[ "${#args[@]}" -eq 0 ]]; then
    args=("menu")
  fi

  echo "[CMD] $LAUNCHER_PATH ${args[*]}"
  "$LAUNCHER_PATH" "${args[@]}"
}

main() {
  print_banner
  parse_args "$@"
  require_admin
  ensure_installer_dependencies
  prepare_directories
  sync_github_repo
  setup_python_runtime
  write_launcher
  run_gsi
  ok "GSI 설치형 작업 완료"
}

main "$@"
