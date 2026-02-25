#!/usr/bin/env bash
set -euo pipefail

GSI_REPO_SSH_URL="git@github.com:iod-kr/GSI.git"
GSI_REPO_HTTPS_URL="https://github.com/iod-kr/GSI.git"
GSI_REPO_WEB_URL="https://github.com/iod-kr/GSI"
GSI_REPO_URL="$GSI_REPO_HTTPS_URL"
GSI_AUTH_MODE="https-anon"
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
  echo -e "${C_BLUE} Auth Mode:    $GSI_AUTH_MODE${C_RESET}"
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

Auth:
  - Default: HTTPS anonymous auth (public repo)
  - Optional: set GSI_GITHUB_TOKEN for non-interactive HTTPS auth (private repo)
      export GSI_GITHUB_TOKEN=<token>
  - Optional: force SSH auth
      export GSI_USE_SSH=1
USAGE
}

select_repo_auth() {
  if [[ -n "${GSI_GITHUB_TOKEN:-}" ]]; then
    GSI_REPO_URL="$GSI_REPO_HTTPS_URL"
    GSI_AUTH_MODE="https-token"
    return
  fi

  if [[ "${GSI_USE_SSH:-0}" == "1" ]]; then
    GSI_REPO_URL="$GSI_REPO_SSH_URL"
    GSI_AUTH_MODE="ssh"
    return
  fi

  GSI_REPO_URL="$GSI_REPO_HTTPS_URL"
  GSI_AUTH_MODE="https-anon"
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
  local commands=(git python3)
  if [[ "$GSI_AUTH_MODE" == "ssh" ]]; then
    commands+=(ssh)
  fi

  for cmd in "${commands[@]}"; do
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

run_git_quiet() {
  local output=""
  local -a git_env=(
    "GIT_TERMINAL_PROMPT=0"
    "GIT_ASKPASS=/bin/false"
    "GIT_SSH_COMMAND=ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new"
  )
  if [[ "$GSI_AUTH_MODE" == "https-token" ]]; then
    git_env+=("GIT_CONFIG_COUNT=1")
    git_env+=("GIT_CONFIG_KEY_0=http.https://github.com/.extraheader")
    git_env+=("GIT_CONFIG_VALUE_0=AUTHORIZATION: bearer ${GSI_GITHUB_TOKEN}")
  fi

  if ! output="$(
    env "${git_env[@]}" git "$@" 2>&1
  )"; then
    err "[E3003] Git 명령 실패: git $*"
    if [[ "$output" == *"Permission denied (publickey)"* ]]; then
      err "[E3004] GitHub SSH 인증 실패: 공개키 권한이 없습니다."
      echo "[HINT] ssh -T git@github.com"
      echo "[HINT] 공개키를 GitHub 계정 SSH Keys 또는 저장소 Deploy Keys에 등록하세요."
      echo "[HINT] 또는 GSI_GITHUB_TOKEN 환경변수를 설정해 HTTPS 토큰 인증으로 실행하세요."
    elif [[ "$output" == *"could not read Username"* ]]; then
      err "[E3005] HTTPS 인증 정보가 없어 비대화형 인증에 실패했습니다."
      echo "[HINT] 공개 저장소가 아니라면 GSI_GITHUB_TOKEN=<token> 설정 후 재실행하세요."
      echo "[HINT] 또는 GSI_USE_SSH=1로 SSH 인증을 사용하세요."
    elif [[ "$output" == *"Repository not found"* ]]; then
      err "[E3006] 저장소를 찾지 못했습니다. 저장소 이름/권한을 확인하세요."
    fi
    if [[ -n "$output" ]]; then
      echo "$output"
    fi
    return 1
  fi
  return 0
}

prepare_directories() {
  step "설치 경로 준비"
  mkdir -p "$INSTALL_ROOT" "$DATA_ROOT" "$LOG_ROOT" "$RUN_ROOT"
  ok "경로 준비 완료"
}

sync_github_repo() {
  step "GitHub 저장소 동기화"

  if [[ -d "$APP_ROOT/.git" ]]; then
    run_git_quiet -C "$APP_ROOT" remote set-url origin "$GSI_REPO_URL" || exit 1
    run_git_quiet -C "$APP_ROOT" fetch --quiet --depth 1 origin "$GSI_REPO_BRANCH" || exit 1
    run_git_quiet -C "$APP_ROOT" checkout -B "$GSI_REPO_BRANCH" "origin/$GSI_REPO_BRANCH" || exit 1
    ok "저장소 업데이트 완료"
    return
  fi

  if [[ -e "$APP_ROOT" && ! -d "$APP_ROOT/.git" ]]; then
    err "[E3002] $APP_ROOT 가 Git 저장소가 아닙니다. 경로를 정리 후 재시도하세요."
    exit 1
  fi

  run_git_quiet clone --quiet --depth 1 --branch "$GSI_REPO_BRANCH" "$GSI_REPO_URL" "$APP_ROOT" || exit 1
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

  local args=("${FORWARD_ARGS[@]}")
  if [[ "${#args[@]}" -eq 0 ]]; then
    args=("menu")
  fi

  "$LAUNCHER_PATH" "${args[@]}"
}

main() {
  parse_args "$@"
  select_repo_auth
  print_banner
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
