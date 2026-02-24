param(
  [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

& $PythonExe -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller

& .\.venv\Scripts\pyinstaller.exe --clean --onefile --console --name gsi .\gsi\__main__.py

Write-Host "Built: $root\dist\gsi.exe"
