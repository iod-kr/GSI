# GSI (Game Server Installer)

CLI 기반 게임 서버 설치기입니다.
- Linux: 터미널(CLI)
- Windows: CMD/PowerShell(CLI)
- **모든 실행은 관리자 권한 필수** (Linux: root, Windows: Administrator)

현재 지원(1차):
- Minecraft
- Valheim
- Counter-Strike 2
- Palworld

## 핵심 기능
- GitHub 연결 설치형 배포/업데이트 (`bootstrap/install.sh`, `bootstrap/install.bat`)
- Docker/Native 모드 선택 설치
- 설치 시 다운로드 자동 진행(Docker 모드: 이미지 pull)
- 게임 서버 버전 선택(`--game-version`)
- 추가 종속성 버전 선택(`--dep-versions`, 예: Java/SteamCMD)
- 인스턴스별 운영 스크립트 자동 생성(`start/stop/update/backup/restore`)
- EULA 자동 동의 옵션(`--auto-eula`)
  - Minecraft는 `eula.txt` 자동 생성(`eula=true`)

## 빠른 시작 (SH 먼저 테스트)
```bash
cd /home/project/gsi
sudo bash bootstrap/install.sh
```
- 고정 GitHub 저장소에서 설치/업데이트 후 실행합니다.
- 옵션 없이 실행하면 배너 + 단계형 대화형 설치 메뉴가 열립니다.
- 설치 완료 후 현재 인스턴스 목록을 바로 확인할 수 있습니다.
- 대화형 메뉴는 아래 10단계로 진행됩니다.
  - 1) 설치기 업데이트 확인(고정 GitHub URL)
  - 2) SDK/의존성 확인
    - 누락 항목이 있으면 자동 설치 시도 여부를 사용자에게 묻고 진행
    - 실패 시 오류 코드/사유를 출력
  - 3) 게임 목록 선택
  - 4) 경로 지정(전체 경로/위치 경로)
  - 5) 서버 폴더 이름
  - 6) 게임별 EULA 동의
  - 7) 설치
  - 8) 네트워크 개방 작업
  - 9) 외부 접속/포트 체크
  - 10) 서버 오픈 알림/서버 쉘
- 업데이트 체크 고정 URL은 `gsi/cli.py`의 `INSTALLER_UPDATE_URL` 상수입니다.
- 설치기 고정 GitHub 저장소 URL은 `bootstrap/install.sh`/`bootstrap/install.bat` 상수에 정의되어 있습니다.

비대화형 예시(설치만, 실행 생략):
```bash
cd /home/project/gsi
sudo bash bootstrap/install.sh --skip-run
```

설치 후 바로 명령 실행:
```bash
sudo bash bootstrap/install.sh -- catalog
sudo bash bootstrap/install.sh -- menu
```

## Windows(CMD) 사용
```bat
cd C:\path\to\gsi
bootstrap\install.bat
```
- 관리자 권한 CMD/PowerShell에서 실행해야 합니다.
- 설치 후 `%ProgramData%\GSI\gsi.cmd` 런처가 생성됩니다.

## 상태/로그 경로
- Linux 기본 data root: `/var/lib/gsi`
- Windows 기본 data root: `%ProgramData%\GSI\data`

## 주의
- `--auto-eula`는 서비스 약관 검토 후 사용하세요.
- Native 모드는 게임별 벤더 정책/런타임에 맞춰 명령을 확장해야 합니다.
