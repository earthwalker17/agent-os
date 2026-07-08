<#
.SYNOPSIS
  Starts Agent OS: the FastAPI backend (port 8000) and the React frontend
  (port 5173), each in its own window, then opens the app in your browser.

.USAGE
  .\start.ps1
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not (Test-Path (Join-Path $Root "backend\.env"))) {
    Write-Host "[!] backend\.env not found — run .\install.ps1 first." -ForegroundColor Yellow
    exit 1
}

# Warn (but don't block) when no provider key is filled in yet.
$envText = Get-Content (Join-Path $Root "backend\.env") -Raw
$hasKey = $false
foreach ($line in ($envText -split "`n")) {
    $t = $line.Trim()
    if ($t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $name, $value = $t.Split("=", 2)
    if ($name -match "API_KEY$" -and $value.Trim() -and $value.Trim() -ne "your-anthropic-key-here") {
        $hasKey = $true; break
    }
}
if (-not $hasKey) {
    Write-Host "[!] No model provider key found in backend\.env — chat will not work" -ForegroundColor Yellow
    Write-Host "    until you add one (e.g. ANTHROPIC_API_KEY)." -ForegroundColor Yellow
}

Write-Host "Starting Agent OS backend  ->  http://localhost:8000" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$Root\backend'; python -m uvicorn main:app --port 8000"
)

Write-Host "Starting Agent OS frontend ->  http://localhost:5173" -ForegroundColor Cyan
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "Set-Location '$Root\frontend'; npm run dev"
)

Start-Sleep -Seconds 3
Start-Process "http://localhost:5173"
Write-Host "Agent OS is starting — the UI opens at http://localhost:5173 once Vite is ready." -ForegroundColor Green
