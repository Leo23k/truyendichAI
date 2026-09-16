param(
    [int]$Port = 8002,
    [switch]$NoReload
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot "venv\Scripts\python.exe"

if (-not (Test-Path $PythonPath)) {
    Write-Error "Missing venv. Run: python -m venv venv; venv\Scripts\python.exe -m pip install -r requirements.txt"
}

$portInUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($portInUse) {
    $pids = ($portInUse | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    Write-Error "Port $Port is already in use by PID(s): $pids. Use -Port 8003 or stop that process."
}

$args = @(
    "-m", "uvicorn",
    "app.main:app",
    "--host", "127.0.0.1",
    "--port", "$Port"
)

if (-not $NoReload) {
    $args += @("--reload", "--reload-dir", "app")
}

Set-Location $ProjectRoot
Write-Host "LeoTruyen local server: http://127.0.0.1:$Port"
& $PythonPath @args
exit $LASTEXITCODE
