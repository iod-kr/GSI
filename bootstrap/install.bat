@echo off
setlocal EnableExtensions EnableDelayedExpansion

set GSI_REPO_SSH_URL=git@github.com:iod-kr/GSI.git
set GSI_REPO_HTTPS_URL=https://github.com/iod-kr/GSI.git
set GSI_REPO_WEB_URL=https://github.com/iod-kr/GSI
set GSI_REPO_URL=%GSI_REPO_HTTPS_URL%
set GSI_AUTH_MODE=https-anon
set GSI_REPO_BRANCH=main
set INSTALL_ROOT=%ProgramData%\GSI
set APP_ROOT=%INSTALL_ROOT%\app
set VENV_DIR=%INSTALL_ROOT%\.venv
set DATA_ROOT=%ProgramData%\GSI\data
set LOG_ROOT=%ProgramData%\GSI\logs
set LAUNCHER=%ProgramData%\GSI\gsi.cmd
set SKIP_RUN=0

set GIT_TERMINAL_PROMPT=0
set GIT_ASKPASS=echo
set GIT_SSH_COMMAND=ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new

if defined GSI_GITHUB_TOKEN (
  set GSI_REPO_URL=%GSI_REPO_HTTPS_URL%
  set GSI_AUTH_MODE=https-token
  set GIT_CONFIG_COUNT=1
  set GIT_CONFIG_KEY_0=http.https://github.com/.extraheader
  set GIT_CONFIG_VALUE_0=AUTHORIZATION: bearer %GSI_GITHUB_TOKEN%
)
if /I "%GSI_USE_SSH%"=="1" (
  set GSI_REPO_URL=%GSI_REPO_SSH_URL%
  set GSI_AUTH_MODE=ssh
  set GIT_CONFIG_COUNT=
  set GIT_CONFIG_KEY_0=
  set GIT_CONFIG_VALUE_0=
)

echo ======================================================================
echo  GSI Installer (.bat) - GitHub Connected
echo  Install Root: %INSTALL_ROOT%
echo  Data Root:    %DATA_ROOT%
echo  Auth Mode:    %GSI_AUTH_MODE%
echo ======================================================================

if /I "%~1"=="--skip-run" (
  set SKIP_RUN=1
  shift
)
if /I "%~1"=="--branch" (
  set GSI_REPO_BRANCH=%~2
  shift
  shift
)
if /I "%~1"=="--data-root" (
  set DATA_ROOT=%~2
  shift
  shift
)
if /I "%~1"=="--help" goto :usage
if /I "%~1"=="-h" goto :usage

echo [STEP] 관리자 권한 확인
net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
  fltmc >nul 2>&1
  if %ERRORLEVEL% neq 0 (
    echo [ERROR][E0001] 관리자 권한이 필요합니다. 관리자 CMD/PowerShell에서 실행하세요.
    exit /b 1
  )
)
echo [OK] 관리자 권한 확인됨

echo [STEP] 설치기 의존성 확인
where git >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo [ERROR][E3001] git 이 필요합니다.
  exit /b 1
)
if /I "%GSI_AUTH_MODE%"=="ssh" (
  where ssh >nul 2>&1
  if %ERRORLEVEL% neq 0 (
    echo [ERROR][E3001] ssh 가 필요합니다.
    exit /b 1
  )
)
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
  where python3 >nul 2>&1
  if %ERRORLEVEL% neq 0 (
    echo [ERROR][E3001] python3 가 필요합니다.
    exit /b 1
  )
  set PYTHON=python3
) else (
  set PYTHON=python
)

echo [STEP] 설치 경로 준비
if not exist "%INSTALL_ROOT%" mkdir "%INSTALL_ROOT%"
if not exist "%DATA_ROOT%" mkdir "%DATA_ROOT%"
if not exist "%LOG_ROOT%" mkdir "%LOG_ROOT%"

echo [STEP] GitHub 저장소 동기화
if exist "%APP_ROOT%\.git" (
  call :run_git -C "%APP_ROOT%" remote set-url origin "%GSI_REPO_URL%"
  if %ERRORLEVEL% neq 0 exit /b 1
  call :run_git -C "%APP_ROOT%" fetch --quiet --depth 1 origin "%GSI_REPO_BRANCH%"
  if %ERRORLEVEL% neq 0 exit /b 1
  call :run_git -C "%APP_ROOT%" checkout -B "%GSI_REPO_BRANCH%" "origin/%GSI_REPO_BRANCH%"
  if %ERRORLEVEL% neq 0 exit /b 1
) else (
  if exist "%APP_ROOT%" (
    echo [ERROR][E3002] %APP_ROOT% 는 Git 저장소가 아닙니다.
    exit /b 1
  )
  call :run_git clone --quiet --depth 1 --branch "%GSI_REPO_BRANCH%" "%GSI_REPO_URL%" "%APP_ROOT%"
  if %ERRORLEVEL% neq 0 exit /b 1
)

echo [STEP] Python 런타임 설정
if not exist "%VENV_DIR%\Scripts\python.exe" (
  %PYTHON% -m venv "%VENV_DIR%"
  if %ERRORLEVEL% neq 0 exit /b 1
)
"%VENV_DIR%\Scripts\python.exe" -m pip install --upgrade pip >nul
if %ERRORLEVEL% neq 0 exit /b 1
"%VENV_DIR%\Scripts\python.exe" -m pip install -r "%APP_ROOT%\requirements.txt" >nul
if %ERRORLEVEL% neq 0 exit /b 1

echo [STEP] 실행 런처 설치
(
  echo @echo off
  echo setlocal EnableExtensions EnableDelayedExpansion
  echo "%VENV_DIR%\Scripts\python.exe" -m gsi --data-root "%DATA_ROOT%" %%*
) > "%LAUNCHER%"
if %ERRORLEVEL% neq 0 exit /b 1

echo [OK] 런처 설치 완료: %LAUNCHER%

if "%SKIP_RUN%"=="1" (
  echo [WARN] --skip-run 옵션으로 실행을 건너뜁니다.
  goto :done
)

if "%~1"=="" (
  "%LAUNCHER%" menu
) else (
  "%LAUNCHER%" %*
)
if %ERRORLEVEL% neq 0 (
  echo [ERROR] GSI 실행 실패 (exit=%ERRORLEVEL%)
  exit /b %ERRORLEVEL%
)

:done
echo [OK] GSI 설치형 작업 완료
exit /b 0

:usage
echo Usage: bootstrap\install.bat [--skip-run] [--branch ^<name^>] [--data-root ^<path^>] [-- ^<gsi args...^>]
echo Auth: default HTTPS anonymous(public), private repo use GSI_GITHUB_TOKEN or GSI_USE_SSH=1
exit /b 0

:run_git
set "_GSI_GIT_LOG=%TEMP%\gsi-git-%RANDOM%-%RANDOM%.log"
git %* >"%_GSI_GIT_LOG%" 2>&1
if %ERRORLEVEL% neq 0 (
  echo [ERROR][E3003] Git 명령 실패: git %*
  findstr /C:"Permission denied (publickey)" "%_GSI_GIT_LOG%" >nul
  if %ERRORLEVEL%==0 (
    echo [ERROR][E3004] GitHub SSH 인증 실패: 공개키 권한이 없습니다.
    echo [HINT] ssh -T git@github.com
    echo [HINT] 공개키를 GitHub 계정 SSH Keys 또는 저장소 Deploy Keys에 등록하세요.
    echo [HINT] 또는 GSI_GITHUB_TOKEN 환경변수를 설정해 HTTPS 토큰 인증으로 실행하세요.
  )
  findstr /C:"could not read Username" "%_GSI_GIT_LOG%" >nul
  if %ERRORLEVEL%==0 (
    echo [ERROR][E3005] HTTPS 인증 정보가 없어 비대화형 인증에 실패했습니다.
    echo [HINT] 공개 저장소가 아니라면 GSI_GITHUB_TOKEN=<token> 설정 후 재실행하세요.
    echo [HINT] 또는 GSI_USE_SSH=1로 SSH 인증을 사용하세요.
  )
  findstr /C:"Repository not found" "%_GSI_GIT_LOG%" >nul
  if %ERRORLEVEL%==0 (
    echo [ERROR][E3006] 저장소를 찾지 못했습니다. 저장소 이름/권한을 확인하세요.
  )
  type "%_GSI_GIT_LOG%"
  del /q "%_GSI_GIT_LOG%" >nul 2>&1
  exit /b 1
)
del /q "%_GSI_GIT_LOG%" >nul 2>&1
exit /b 0
