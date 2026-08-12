param(
    [string]$OutputDir = "dist",
    [string]$BundleName = "cairnival-local-installer",
    [switch]$SkipZip
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is not on PATH. Install Python 3.11+ and retry."
}

$bundleRoot = Join-Path $repoRoot (Join-Path $OutputDir $BundleName)
$wheelhouse = Join-Path $bundleRoot "wheelhouse"

if (Test-Path $bundleRoot) {
    Remove-Item -Recurse -Force $bundleRoot
}
New-Item -ItemType Directory -Path $wheelhouse -Force | Out-Null

Write-Host "Building Cairnival wheel and dependency wheels ..."
python -m pip wheel . --wheel-dir "$wheelhouse" | Out-Host

if (-not (Test-Path (Join-Path $wheelhouse "cairnival-*.whl"))) {
    throw "Failed to build Cairnival wheel in $wheelhouse"
}

Copy-Item -Path (Join-Path $repoRoot ".env.example") -Destination (Join-Path $bundleRoot ".env.example") -Force

$runPs1Template = @'
param(
    [ValidateSet("agent", "hub", "once", "status")]
    [string]$Mode = "agent"
)

$ErrorActionPreference = "Stop"
$installDir = "__INSTALL_DIR__"
$venvPython = "__VENV_PY__"

Set-Location $installDir

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (Test-Path ".env") {
    Get-Content ".env" | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $pair = $line -split "=", 2
        if ($pair.Count -ne 2) { return }
        $key = $pair[0].Trim()
        $value = $pair[1].Trim()
        [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}

if (-not $env:CAIRNIVAL_HOME) {
    $env:CAIRNIVAL_HOME = Join-Path $installDir "data"
}

if (-not $env:LLM_BACKEND) {
    $env:LLM_BACKEND = "echo"
}

& $venvPython -m cairnival.cli $Mode
exit $LASTEXITCODE
'@

$runCmdTemplate = @'
@echo off
setlocal EnableExtensions EnableDelayedExpansion
set "MODE=%~1"
if "%MODE%"=="" set "MODE=agent"

cd /d "__INSTALL_DIR__"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist ".env" (
  for /f "usebackq delims=" %%L in (".env") do (
    set "line=%%L"
    if not "!line!"=="" (
      if not "!line:~0,1!"=="#" (
        for /f "tokens=1* delims==" %%A in ("!line!") do (
          set "%%A=%%B"
        )
      )
    )
  )
)

if not defined CAIRNIVAL_HOME set "CAIRNIVAL_HOME=__INSTALL_DIR__\data"
if not defined LLM_BACKEND set "LLM_BACKEND=echo"

"__VENV_PY__" -m cairnival.cli %MODE%
exit /b %errorlevel%
'@

$installPs1 = @'
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\Cairnival",
    [string]$VenvDir = ".venv"
)

$ErrorActionPreference = "Stop"

$bundleRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$wheelhouse = Join-Path $bundleRoot "wheelhouse"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is not on PATH. Install Python 3.11+ and retry."
}

if (-not (Test-Path $wheelhouse)) {
    throw "wheelhouse not found: $wheelhouse"
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$venvPath = Join-Path $InstallDir $VenvDir
$venvPython = Join-Path $venvPath "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment at $venvPath ..."
    python -m venv "$venvPath"
}

Write-Host "Installing Cairnival from local wheelhouse ..."
& $venvPython -m pip install --upgrade pip | Out-Host
& $venvPython -m pip install --no-index --find-links "$wheelhouse" cairnival | Out-Host

$envExample = Join-Path $bundleRoot ".env.example"
$envTarget = Join-Path $InstallDir ".env"
if ((Test-Path $envExample) -and -not (Test-Path $envTarget)) {
    Copy-Item $envExample $envTarget
    Write-Host "Wrote default env file: $envTarget"
}

$runPs1Template = Join-Path $bundleRoot "run-cairnival.template.ps1"
$runCmdTemplate = Join-Path $bundleRoot "run-cairnival.template.cmd"
if (-not (Test-Path $runPs1Template)) {
    throw "Missing template: $runPs1Template"
}
if (-not (Test-Path $runCmdTemplate)) {
    throw "Missing template: $runCmdTemplate"
}

$resolvedInstallDir = (Resolve-Path $InstallDir).Path

$ps1Content = (Get-Content $runPs1Template -Raw).
    Replace("__INSTALL_DIR__", $resolvedInstallDir).
    Replace("__VENV_PY__", $venvPython)
$cmdContent = (Get-Content $runCmdTemplate -Raw).
    Replace("__INSTALL_DIR__", $resolvedInstallDir).
    Replace("__VENV_PY__", $venvPython)

$runPs1Path = Join-Path $InstallDir "run-cairnival.ps1"
$runCmdPath = Join-Path $InstallDir "run-cairnival.cmd"

Set-Content -Path $runPs1Path -Value $ps1Content -Encoding UTF8
Set-Content -Path $runCmdPath -Value $cmdContent -Encoding ASCII

Write-Host "Installed to: $resolvedInstallDir"
Write-Host "Run with:"
Write-Host "  PowerShell: $runPs1Path -Mode agent"
Write-Host "  CMD:        $runCmdPath agent"
'@

$installCmd = @'
@echo off
set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install-local.ps1" %*
exit /b %errorlevel%
'@

$notes = @'
Cairnival Local Installer Bundle
================================

Contents:
- wheelhouse\*.whl (Cairnival + dependencies)
- install-local.ps1 (installer)
- install-local.cmd (CMD launcher for installer)
- run-cairnival.template.ps1 / .cmd (launcher templates)
- .env.example

Install:
1) Open this folder in PowerShell or CMD.
2) Run install-local.cmd
   Optional: install-local.cmd -InstallDir "D:\Apps\Cairnival"

After install:
- PowerShell: <InstallDir>\run-cairnival.ps1 -Mode agent
- CMD:        <InstallDir>\run-cairnival.cmd agent
'@

Set-Content -Path (Join-Path $bundleRoot "install-local.ps1") -Value $installPs1 -Encoding UTF8
Set-Content -Path (Join-Path $bundleRoot "install-local.cmd") -Value $installCmd -Encoding ASCII
Set-Content -Path (Join-Path $bundleRoot "run-cairnival.template.ps1") -Value $runPs1Template -Encoding UTF8
Set-Content -Path (Join-Path $bundleRoot "run-cairnival.template.cmd") -Value $runCmdTemplate -Encoding ASCII
Set-Content -Path (Join-Path $bundleRoot "README-INSTALLER.txt") -Value $notes -Encoding ASCII

if (-not $SkipZip) {
    $zipPath = Join-Path $repoRoot (Join-Path $OutputDir "$BundleName.zip")
    if (Test-Path $zipPath) {
        Remove-Item $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $bundleRoot "*") -DestinationPath $zipPath
    Write-Host "Created: $zipPath"
}

Write-Host "Bundle directory ready: $bundleRoot"
