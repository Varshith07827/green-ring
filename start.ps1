# Starts the whole thing: the OpenWA gateway and the bridge.
#
#   .\start.ps1
#
# Then open http://localhost:2785, start the session once and scan the QR.
# After that, POST {"id": "...", "msg": "..."} to http://localhost:8000/send

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$repo = Join-Path $root "repo"
$bridge = Join-Path $root "bridge"
$python = Join-Path $bridge ".venv\Scripts\python.exe"

$env:Path = "C:\Program Files\nodejs;C:\Program Files\Git\usr\bin;" + $env:Path

foreach ($file in @((Join-Path $repo ".env"), (Join-Path $bridge ".env"))) {
    if (-not (Test-Path $file)) { Write-Error "$file is missing. See README.md." }
}

# ---- first run: install and build ----------------------------------------

if (-not (Test-Path (Join-Path $repo "node_modules"))) {
    Write-Host "Installing gateway dependencies (first run, a few minutes)..." -ForegroundColor Cyan
    Push-Location $repo
    # --ignore-scripts is required on Windows: npm would otherwise run node-gyp
    # for better-sqlite3, which needs Visual Studio C++ Build Tools. The package
    # ships a prebuilt win32-x64 binary its loader prefers anyway. postinstall
    # is then run by hand for the upstream whatsapp-web.js/baileys patches.
    npm ci --ignore-scripts
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "npm ci failed" }
    node scripts/postinstall.js
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "postinstall failed" }
    Pop-Location
}

if (-not (Test-Path (Join-Path $repo "dist\main.js"))) {
    Write-Host "Building the gateway (first run only)..." -ForegroundColor Cyan
    Push-Location $repo
    npm run build
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "build failed" }
    Pop-Location
}

if (-not (Test-Path (Join-Path $repo "dashboard\dist"))) {
    Write-Host "Building the dashboard (first run only)..." -ForegroundColor Cyan
    Push-Location $repo
    npm run dashboard:build
    if ($LASTEXITCODE -ne 0) { Write-Warning "dashboard build failed - the API still works, the UI will not" }
    Pop-Location
}

if (-not (Test-Path $python)) {
    Write-Host "Creating the Python environment (first run only)..." -ForegroundColor Cyan
    Push-Location $bridge
    python -m venv .venv
    & $python -m pip install --upgrade pip --quiet
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Pop-Location; Write-Error "pip install failed" }
    Pop-Location
}

# ---- start both processes ------------------------------------------------

Write-Host "Starting the OpenWA gateway..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle='OpenWA gateway'; Set-Location '$repo'; " +
    "`$env:Path='C:\Program Files\nodejs;' + `$env:Path; node dist/main"
)

# The bridge creates the session and registers the event subscription at
# startup, so it should come up second - otherwise it just logs a warning and
# you would have to call POST /events/register yourself.
Write-Host "Waiting for the gateway to come up..." -NoNewline
$ready = $false
foreach ($attempt in 1..60) {
    Start-Sleep -Seconds 2
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:2785/api/sessions" -TimeoutSec 3 -UseBasicParsing | Out-Null
        $ready = $true
        break
    }
    catch {
        # 401 means it is up and asking for a key, which is all we need to know.
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode.value__ -eq 401) {
            $ready = $true
            break
        }
        Write-Host "." -NoNewline
    }
}
Write-Host ""

if (-not $ready) {
    Write-Warning "The gateway did not answer within 2 minutes. Check its window for errors."
}

Write-Host "Starting the bridge..." -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$host.UI.RawUI.WindowTitle='OpenWA bridge'; Set-Location '$bridge'; " +
    "& '$python' -m app.main"
)

Start-Sleep -Seconds 4

Write-Host ""
Write-Host "  Running." -ForegroundColor Green
Write-Host ""
Write-Host "  1. Open the dashboard:  " -NoNewline; Write-Host "http://localhost:2785" -ForegroundColor Yellow
Write-Host "     Start the 'default' session and scan the QR. Once only - it is"
Write-Host "     remembered across restarts."
Write-Host ""
Write-Host "  2. Then send messages:  " -NoNewline; Write-Host "POST http://localhost:8000/send" -ForegroundColor Yellow
Write-Host "     header  X-API-Key: <BRIDGE_API_KEY from bridge\.env>"
Write-Host "     body    {`"id`": `"919876543210`", `"msg`": `"hello`"}"
Write-Host ""
Write-Host "  Check readiness any time:  curl.exe http://localhost:8000/health" -ForegroundColor DarkGray
Write-Host "  Both processes run in their own windows. Close them to stop." -ForegroundColor DarkGray
Write-Host ""
