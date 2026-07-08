<#
.SYNOPSIS
  Agent OS one-command installer for Windows.

.DESCRIPTION
  Checks (and offers to install) the prerequisites, clones the repository,
  installs backend + frontend dependencies, prepares your .env, and tells you
  exactly what to do next. Safe to re-run at any time — every step is
  idempotent. Run it from inside an existing checkout to just (re)install
  dependencies ("setup mode").

.USAGE
  From anywhere (installs into .\agent-os):
    irm https://raw.githubusercontent.com/earthwalker17/agent-os/main/install.ps1 | iex

  Choose the install directory:
    $env:AGENT_OS_DIR = "D:\code\agent-os"
    irm https://raw.githubusercontent.com/earthwalker17/agent-os/main/install.ps1 | iex

.NOTES
  Prerequisites handled: Git, Python 3.10+, Node.js 18+ (installed via winget
  with your consent when missing). Nothing outside the install directory is
  modified except those tool installations.
#>

$ErrorActionPreference = "Stop"
$RepoUrl = "https://github.com/earthwalker17/agent-os.git"

function Write-Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Info($msg)  { Write-Host "  $msg" -ForegroundColor Gray }
function Write-Warn2($msg) { Write-Host "  [!] $msg" -ForegroundColor Yellow }
function Fail($msg)        { Write-Host "`n[x] $msg" -ForegroundColor Red; exit 1 }

function Update-PathFromRegistry {
    # Pick up tools installed moments ago without reopening the terminal.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Get-ToolVersion($exe, $versionArgs) {
    try {
        $cmd = Get-Command $exe -ErrorAction Stop
        $raw = & $cmd.Source $versionArgs 2>&1 | Select-Object -First 1
        return [string]$raw
    } catch { return $null }
}

function Install-WithWinget($displayName, $wingetId) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Warn2 "$displayName is missing and winget is not available."
        Write-Info  "Install winget from the Microsoft Store ('App Installer'),"
        Write-Info  "or install $displayName manually, then re-run this script."
        Fail "Missing prerequisite: $displayName"
    }
    $answer = Read-Host "  Install $displayName now via winget? [Y/n]"
    if ($answer -and $answer.Trim().ToLower().StartsWith("n")) {
        Fail "Missing prerequisite: $displayName (declined). Install it manually and re-run."
    }
    winget install --id $wingetId --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) { Fail "winget failed to install $displayName (exit $LASTEXITCODE)." }
    Update-PathFromRegistry
}

Write-Host ""
Write-Host "  Agent OS installer" -ForegroundColor White
Write-Host "  local-first AI project operating system" -ForegroundColor DarkGray

# ---------------------------------------------------------------- prerequisites
Write-Step "Checking prerequisites"

# Git
if (-not (Get-ToolVersion "git" "--version")) {
    Install-WithWinget "Git" "Git.Git"
}
Write-Ok ("git: " + (Get-ToolVersion "git" "--version"))

# Python 3.10+
function Test-Python {
    $v = Get-ToolVersion "python" "--version"      # "Python 3.12.4"
    if (-not $v) { return $null }
    if ($v -match "Python (\d+)\.(\d+)") {
        if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10)) { return $v }
    }
    return $null
}
if (-not (Test-Python)) {
    Write-Warn2 "Python 3.10+ not found on PATH."
    Install-WithWinget "Python 3.12" "Python.Python.3.12"
    if (-not (Test-Python)) {
        Fail "Python installed but not on PATH yet — open a new terminal and re-run this script."
    }
}
Write-Ok ("python: " + (Test-Python))

# Node 18+
function Test-Node {
    $v = Get-ToolVersion "node" "--version"        # "v20.11.1"
    if ($v -and $v -match "v(\d+)\." -and [int]$Matches[1] -ge 18) { return $v }
    return $null
}
if (-not (Test-Node)) {
    Write-Warn2 "Node.js 18+ not found on PATH."
    Install-WithWinget "Node.js LTS" "OpenJS.NodeJS.LTS"
    if (-not (Test-Node)) {
        Fail "Node.js installed but not on PATH yet — open a new terminal and re-run this script."
    }
}
Write-Ok ("node: " + (Test-Node) + "  npm: " + (Get-ToolVersion "npm" "--version"))

# ---------------------------------------------------------------- repository
Write-Step "Locating the repository"

if ((Test-Path ".\backend\main.py") -and (Test-Path ".\frontend\package.json")) {
    $RepoDir = (Get-Location).Path
    Write-Ok "Existing checkout detected — running in setup mode: $RepoDir"
} else {
    $RepoDir = if ($env:AGENT_OS_DIR) { $env:AGENT_OS_DIR } else { Join-Path (Get-Location).Path "agent-os" }
    if (Test-Path (Join-Path $RepoDir "backend\main.py")) {
        Write-Ok "Existing checkout found: $RepoDir"
    } else {
        Write-Info "Cloning $RepoUrl -> $RepoDir"
        git clone $RepoUrl $RepoDir
        if ($LASTEXITCODE -ne 0) { Fail "git clone failed (exit $LASTEXITCODE)." }
        Write-Ok "Cloned."
    }
    Set-Location $RepoDir
}

# ---------------------------------------------------------------- backend deps
Write-Step "Installing backend dependencies (Python)"
python -m pip install --disable-pip-version-check -r backend\requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (exit $LASTEXITCODE)." }
Write-Ok "Python packages installed."

Write-Step "Installing the Playwright Chromium browser (used for browser verification)"
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Fail "playwright install failed (exit $LASTEXITCODE)." }
Write-Ok "Chromium ready."

# ---------------------------------------------------------------- frontend deps
Write-Step "Installing frontend dependencies (npm)"
Push-Location frontend
npm install --no-fund --no-audit
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "npm install failed (exit $LASTEXITCODE)." }
Pop-Location
Write-Ok "npm packages installed."

# ---------------------------------------------------------------- .env
Write-Step "Preparing your environment file"
if (Test-Path "backend\.env") {
    Write-Ok "backend\.env already exists — left untouched."
} else {
    Copy-Item "backend\.env.example" "backend\.env"
    Write-Ok "Created backend\.env from the template."
}

# ---------------------------------------------------------------- done
Write-Host ""
Write-Host "  Agent OS is installed." -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps" -ForegroundColor White
Write-Host "    1. Open backend\.env and add at least one model provider key" -ForegroundColor Gray
Write-Host "       (ANTHROPIC_API_KEY recommended). Connector tokens (GitHub /" -ForegroundColor DarkGray
Write-Host "       Vercel / Supabase / Stripe test / Tavily) are optional and" -ForegroundColor DarkGray
Write-Host "       unlock delivery + research features." -ForegroundColor DarkGray
Write-Host "    2. Start Agent OS:   .\start.ps1" -ForegroundColor Gray
Write-Host "    3. Open              http://localhost:5173" -ForegroundColor Gray
Write-Host ""
