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

# ---- prerequisites -------------------------------------------------------
# Node, Python and Chrome have to exist before anything else can run. Without
# this check the first sign of trouble is a raw "npm is not recognized" three
# steps later, which says nothing about what to do.

function Update-PathFromRegistry {
    # A fresh install writes PATH to the registry, but this already-running
    # shell keeps the copy it started with - so node stays "not recognized"
    # until the variable is re-read.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "C:\Program Files\nodejs;C:\Program Files\Git\usr\bin;$machine;$user"
}

function Get-NodeVersion {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    try { return [int](((& node -v) -replace '^v', '') -split '\.')[0] } catch { return $null }
}

function Get-PythonMinor {
    # `python` on Windows is often the Store stub, which prints nothing useful
    # and opens the Store instead. Judge it by what it actually reports.
    try { $out = (& python --version 2>&1) | Out-String } catch { return $null }
    if ($out -match 'Python 3\.(\d+)') { return [int]$Matches[1] }
    return $null
}

function Find-Chrome {
    @(
        "C:\Program Files\Google\Chrome\Application\chrome.exe",
        "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Get-MissingPrereqs {
    $missing = @()
    $node = Get-NodeVersion
    if ($null -eq $node) { $missing += @{ Name = "Node.js"; Id = "OpenJS.NodeJS.LTS"; Why = "not installed" } }
    elseif ($node -lt 22) { $missing += @{ Name = "Node.js"; Id = "OpenJS.NodeJS.LTS"; Why = "v$node is too old, needs 22+" } }

    $py = Get-PythonMinor
    if ($null -eq $py) { $missing += @{ Name = "Python"; Id = "Python.Python.3.12"; Why = "not installed" } }
    elseif ($py -lt 10) { $missing += @{ Name = "Python"; Id = "Python.Python.3.12"; Why = "3.$py is too old, needs 3.10+" } }

    if (-not (Find-Chrome)) { $missing += @{ Name = "Google Chrome"; Id = "Google.Chrome"; Why = "not installed" } }
    return $missing
}

$missing = Get-MissingPrereqs
if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Host "  Missing prerequisites:" -ForegroundColor Yellow
    foreach ($m in $missing) { Write-Host "    - $($m.Name)  ($($m.Why))" }
    Write-Host ""

    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "  winget is not available, so these cannot be installed automatically." -ForegroundColor DarkGray
        Write-Host "    Node.js         https://nodejs.org/          (22 LTS or newer)" -ForegroundColor DarkGray
        Write-Host "    Python          https://www.python.org/      (3.10 or newer)" -ForegroundColor DarkGray
        Write-Host "    Google Chrome   https://www.google.com/chrome/" -ForegroundColor DarkGray
        Write-Error "Install the above, then run this again."
    }

    $answer = Read-Host "  Install them now with winget? [Y/n]"
    if ($answer -and $answer -notmatch '^(y|yes)$') {
        Write-Error "Cannot continue without them."
    }

    Write-Host "  Windows will ask for permission for each one." -ForegroundColor DarkGray
    foreach ($m in $missing) {
        Write-Host "  installing $($m.Name)..." -ForegroundColor Cyan
        winget install --id $m.Id --silent --accept-package-agreements --accept-source-agreements
        # winget reports "no applicable upgrade" as a failure; the re-check
        # below is what actually decides, so a non-zero code is not fatal here.
    }

    Update-PathFromRegistry
    $missing = Get-MissingPrereqs
    if ($missing.Count -gt 0) {
        Write-Host ""
        foreach ($m in $missing) { Write-Warning "$($m.Name) is still $($m.Why)" }
        Write-Error "Close this window, open a new one, and run .\start.ps1 again - a fresh install often needs a new terminal before it is visible."
    }
    Write-Host "  all prerequisites present." -ForegroundColor Green
    Write-Host ""
}

# ---- configuration -------------------------------------------------------
# One file, at the project root, gitignored because it holds secrets. The
# gateway's own repo\.env is GENERATED from it below on every run - so the key
# they share cannot drift apart, and machine facts like the Chrome path never
# have to be typed by hand.

$rootEnv = Join-Path $root ".env"
$repoEnv = Join-Path $repo ".env"

function Read-EnvFile([string]$path) {
    $map = @{}
    if (Test-Path $path) {
        foreach ($line in Get-Content $path) {
            if ($line -match '^([A-Z_]+)=(.*)$') { $map[$Matches[1]] = $Matches[2] }
        }
    }
    return $map
}

function New-Secret([int]$bytes) {
    $b = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    return ($b | ForEach-Object { $_.ToString("x2") }) -join ""
}

if (-not (Test-Path $rootEnv)) {
    Write-Host ""
    Write-Host "  First run - setting up configuration." -ForegroundColor Cyan
    Write-Host ""

    $mongo = Read-Host "  MongoDB URI [mongodb://localhost:27017]"
    if (-not $mongo) { $mongo = "mongodb://localhost:27017" }
    $pollUrl = Read-Host "  Queue URL to poll, blank to skip (e.g. https://<worker>.workers.dev/wam)"
    $pollToken = ""
    if ($pollUrl) { $pollToken = Read-Host "  Bearer token for that URL" }

    $bridgeKey = New-Secret 24

    # The gateway key is a SEED, not an override. On its first boot the gateway
    # copies API_MASTER_KEY into its own database and writes it to
    # repo\data\.api-key; from then on it authenticates against the database and
    # ignores the setting entirely. So if that file already exists - a gateway
    # that ran before this .env did - inventing a fresh key here would hand the
    # bridge a credential the gateway has never heard of, and every call would
    # 401. Adopt the key it actually seeded instead.
    $bootstrapKey = Join-Path $repo "data\.api-key"
    if (Test-Path $bootstrapKey) {
        $gatewayKey = (Get-Content $bootstrapKey -Raw).Trim()
        Write-Host "  Adopting the gateway key it already seeded (repo\data\.api-key)." -ForegroundColor DarkGray
    }
    else {
        $gatewayKey = New-Secret 32
    }
@"
# ===========================================================================
# Configuration - this is the only file you edit.
#
# repo\.env is generated from this one by start.ps1 on every run. Don't edit
# that; your changes get overwritten.
# ===========================================================================

# --- Set these -------------------------------------------------------------

# Where every message is archived, both directions.
MONGO_URI=$mongo

# The queue the bridge collects outgoing messages from, so you can send from
# any machine. Leave both blank to turn that off and send only from this one.
POLL_URL=$pollUrl
POLL_TOKEN=$pollToken

# --- Generated once - keep them secret, no need to change them -------------

# What you send from Postman:  Authorization: Bearer <this>
BRIDGE_API_KEY=$bridgeKey

# Shared with the WhatsApp gateway. start.ps1 copies it into repo\.env.
OPENWA_API_KEY=$gatewayKey

# Signs the gateway's internal event deliveries to the bridge.
EVENTS_SECRET=$(New-Secret 24)

# --- Optional --------------------------------------------------------------
# Everything else has a working default and only belongs here if you are
# changing it. The common ones:
#
#   BRIDGE_PORT=8000            the port Postman talks to
#   BRIDGE_HOST=0.0.0.0         127.0.0.1 to refuse everything but this machine
#   POLL_INTERVAL=3             seconds between queue checks
#   DEFAULT_COUNTRY_CODE=       e.g. 91 to accept bare 10-digit numbers
#   LOG_LEVEL=info              debug for more detail
#   MONGO_DB=openwa             database name
#   OPENWA_SESSION_NAME=default which WhatsApp session to drive
"@ | Set-Content -Path $rootEnv -Encoding utf8

    Write-Host "  wrote .env" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Your key for sending messages (Authorization: Bearer ...):" -ForegroundColor Cyan
    Write-Host "  $bridgeKey" -ForegroundColor Yellow
    Write-Host ""
}

# ---- regenerate the gateway's config from it, every run ------------------

$conf = Read-EnvFile $rootEnv
$gatewayKey = $conf["OPENWA_API_KEY"]
if (-not $gatewayKey) { Write-Error "OPENWA_API_KEY is missing from .env" }

# ---- MongoDB -------------------------------------------------------------
# Only worth offering when the URI points at this machine. A remote or Atlas
# URI is somebody else's server, and installing one locally would be both
# useless and surprising.
$mongoUri = $conf["MONGO_URI"]
if ($mongoUri -match '^mongodb(\+srv)?://(localhost|127\.0\.0\.1)') {
    $port = 27017
    if ($mongoUri -match ':(\d{2,5})') { $port = [int]$Matches[1] }
    if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
        Write-Host ""
        Write-Warning "MONGO_URI points at this machine, but nothing is listening on port $port."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            $answer = Read-Host "  Install MongoDB Community Server now? [Y/n]"
            if (-not $answer -or $answer -match '^(y|yes)$') {
                Write-Host "  installing MongoDB (this one is a few hundred MB)..." -ForegroundColor Cyan
                winget install --id MongoDB.Server --silent --accept-package-agreements --accept-source-agreements
                Update-PathFromRegistry
                # The installer registers a service that takes a moment to listen.
                foreach ($i in 1..15) {
                    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { break }
                    Start-Sleep -Seconds 2
                }
            }
        }
        if (-not (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
            Write-Warning "Still nothing on port $port. The bridge will not start until MONGO_URI reaches a running MongoDB."
            Write-Host "  Either install it, start the service, or point MONGO_URI in .env at another server." -ForegroundColor DarkGray
        }
        Write-Host ""
    }
}

# The gateway only ever SEEDS from API_MASTER_KEY, on its very first boot, and
# authenticates against its database afterwards. Once repo\data\.api-key exists,
# that file - not .env - is the key the gateway actually accepts. If the two
# disagree, every call from the bridge 401s, and nothing in the logs says why.
$bootstrapKey = Join-Path $repo "data\.api-key"
if (Test-Path $bootstrapKey) {
    $seeded = (Get-Content $bootstrapKey -Raw).Trim()
    if ($seeded -and $seeded -ne $gatewayKey) {
        Write-Host ""
        Write-Warning "OPENWA_API_KEY in .env is not the key this gateway accepts."
        Write-Host "  .env has                : $($gatewayKey.Substring(0, [Math]::Min(12, $gatewayKey.Length)))..." -ForegroundColor DarkGray
        Write-Host "  the gateway seeded      : $($seeded.Substring(0, [Math]::Min(12, $seeded.Length)))..." -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  The gateway takes API_MASTER_KEY only on its FIRST boot, then uses its own" -ForegroundColor DarkGray
        Write-Host "  database. Yours has already booted, so .env is being ignored." -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "  Fix it by putting the seeded key in .env:" -ForegroundColor Cyan
        Write-Host "    OPENWA_API_KEY=$seeded" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Or, to start the gateway over from scratch, delete repo\data - which also" -ForegroundColor DarkGray
        Write-Host "  unpairs WhatsApp and means scanning the QR again." -ForegroundColor DarkGray
        Write-Error "Refusing to start with a key the gateway will reject."
    }
}

# Chrome is required: dependencies install without Puppeteer's own copy.
$chrome = @(
    "C:\Program Files\Google\Chrome\Application\chrome.exe",
    "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { Write-Error "Google Chrome not found. Install it, then run this again." }

@"
# OpenWA gateway - local configuration, generated by start.ps1
PORT=2785
NODE_ENV=production
AUTO_START_SESSIONS=true

DATABASE_TYPE=sqlite
DATABASE_NAME=./data/openwa.sqlite
DATABASE_SYNCHRONIZE=true
DATABASE_LOGGING=false

ENGINE_TYPE=whatsapp-web.js
SESSION_DATA_PATH=./data/sessions
PUPPETEER_HEADLESS=true
PUPPETEER_ARGS=--no-sandbox,--disable-setuid-sandbox,--disable-dev-shm-usage
PUPPETEER_EXECUTABLE_PATH=$chrome

# Send the full contact record on inbound messages - id, number, shortName,
# business flags - instead of just { name, pushName }. It is read from the
# already-cached contact, so it costs no extra WhatsApp lookups.
WEBHOOK_CONTACT_DETAILS=true
# Attach `senderPhone` when a sender is identified by a privacy id (@lid)
# rather than a number, so a real number can still be recorded.
RESOLVE_LID_TO_PHONE=true
# Keep a durable copy of inbound media, so it survives past the inline copy.
CHAT_MEDIA_ARCHIVE_ENABLED=true

WEBHOOK_TIMEOUT=10000
WEBHOOK_RETRY_DELAY=5000
WEBHOOK_SHUTDOWN_DRAIN_MS=15000
# The bridge listens on loopback, which the SSRF guard blocks by default.
# Without this allowlist the gateway cannot deliver events to it at all.
SSRF_ALLOWED_HOSTS=localhost,127.0.0.1

STORAGE_TYPE=local
STORAGE_LOCAL_PATH=./data/media

REDIS_ENABLED=false
QUEUE_ENABLED=false
CACHE_ENABLED=false

API_MASTER_KEY=$gatewayKey
CSP_UPGRADE_INSECURE_REQUESTS=false
"@ | Set-Content -Path $repoEnv -Encoding utf8

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
Write-Host "     body    {`"id`": `"919876543210`", `"msg`": `"hello`"}"
# Reprinted here because on a first run it scrolled past several minutes of
# npm output, and it is the one value needed to send anything.
Write-Host "     header  " -NoNewline
Write-Host "Authorization: Bearer $($conf['BRIDGE_API_KEY'])" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Check readiness any time:  curl.exe http://localhost:8000/health" -ForegroundColor DarkGray
Write-Host "  Both processes run in their own windows. Close them to stop." -ForegroundColor DarkGray
Write-Host ""
