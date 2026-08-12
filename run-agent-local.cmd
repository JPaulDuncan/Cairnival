@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "MODE=%~1"
if "%MODE%"=="" set "MODE=agent"

set "SKIP_INSTALL=0"
if /I "%~2"=="--skip-install" set "SKIP_INSTALL=1"
if /I "%~3"=="--skip-install" set "SKIP_INSTALL=1"

set "REPO_ROOT=%~dp0"
cd /d "%REPO_ROOT%"

rem UTF-8 for Python and its subprocesses, so a model's em-dashes and curly
rem quotes survive instead of decoding as "â€".
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not on PATH. Install Python 3.11+ and retry.
  exit /b 1
)

set "VENV_DIR=.venv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo Creating virtual environment at %VENV_DIR% ...
  python -m venv "%VENV_DIR%"
  if errorlevel 1 exit /b %errorlevel%
)

if "%SKIP_INSTALL%"=="0" (
  echo Installing local package into virtual environment ...
  "%VENV_PY%" -m pip install -e .
  if errorlevel 1 exit /b %errorlevel%
)

if exist ".env" (
  echo Loading environment from .env ...
  for /f "usebackq delims=" %%L in (".env") do (
    set "line=%%L"
    if not "!line!"=="" (
      if not "!line:~0,1!"=="#" (
        for /f "tokens=1* delims==" %%A in ("!line!") do (
          set "k=%%A"
          set "v=%%B"
          for /f "tokens=* delims= " %%K in ("!k!") do set "k=%%K"
          if defined k set "!k!=!v!"
        )
      )
    )
  )
)

if not defined CAIRNIVAL_HOME set "CAIRNIVAL_HOME=%REPO_ROOT%data"
if not defined LLM_BACKEND set "LLM_BACKEND=echo"

echo Starting Cairnival in mode: %MODE%
"%VENV_PY%" -m cairnival.cli %MODE%
exit /b %errorlevel%
