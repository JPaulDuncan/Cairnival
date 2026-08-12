param(
    [ValidateSet("agent", "hub", "once", "status")]
    [string]$Mode = "agent",
    [string]$VenvPath = ".venv",
    [string]$EnvFile = ".env",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $repoRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python is not on PATH. Install Python 3.11+ and retry."
}

$venvPython = Join-Path $repoRoot "$VenvPath\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment at $VenvPath ..."
    python -m venv $VenvPath
}

if (-not $SkipInstall) {
    Write-Host "Installing local package into virtual environment ..."
    & $venvPython -m pip install -e . | Out-Host
}

if (Test-Path $EnvFile) {
    Write-Host "Loading environment from $EnvFile ..."
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $pair = $line -split "=", 2
        if ($pair.Count -ne 2) { return }
        $key = $pair[0].Trim()
        $value = $pair[1].Trim()
        if ($value.StartsWith('"') -and $value.EndsWith('"') -and $value.Length -ge 2) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [System.Environment]::SetEnvironmentVariable($key, $value, "Process")
    }
}

if (-not $env:CAIRNIVAL_HOME) {
    $env:CAIRNIVAL_HOME = Join-Path $repoRoot "data"
}

if (-not $env:LLM_BACKEND) {
    # Safe default for first local boot without Ollama/llama.cpp.
    $env:LLM_BACKEND = "echo"
}

Write-Host "Starting Cairnival in mode: $Mode"
& $venvPython -m cairnival.cli $Mode
exit $LASTEXITCODE
