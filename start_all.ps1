# Cognetra 3.0 — PowerShell Launcher
$ErrorActionPreference = "Continue"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "       COGNETRA 3.0 - SYSTEM LAUNCH (PowerShell)        " -ForegroundColor Cyan
Write-Host "  Zero-Shot Accident Detection Platform                 " -ForegroundColor White
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$root = $PSScriptRoot

# 1. Launch Central Server in a separate window
Write-Host "[1/2] Starting Central Server on port 9000..." -ForegroundColor Yellow
Start-Process -FilePath "cmd.exe" -ArgumentList "/k cd /d `"$root\central_server`" && call .venv\Scripts\activate.bat && python app.py"

# Wait for server
Write-Host "Waiting 6 seconds for server to initialize..." -ForegroundColor Gray
Start-Sleep -Seconds 6

# 2. Open Dashboards
Write-Host "[*] Opening dashboards in default browser..." -ForegroundColor Green
Start-Process "http://127.0.0.1:9000/"
Start-Sleep -Seconds 1
Start-Process "http://127.0.0.1:9000/monitor"

# 3. Launch Edge Client in a separate window
Write-Host "[2/2] Starting Edge Client..." -ForegroundColor Yellow
Start-Process -FilePath "cmd.exe" -ArgumentList "/k cd /d `"$root\edge_client`" && call .venv\Scripts\activate.bat && python main.py"

Write-Host ""
Write-Host "========================================================" -ForegroundColor Green
Write-Host "  [OK] Both processes launched in separate windows!" -ForegroundColor Green
Write-Host ""
Write-Host "  Backend Ops Dashboard : http://127.0.0.1:9000/" -ForegroundColor White
Write-Host "  Frontend Monitor      : http://127.0.0.1:9000/monitor" -ForegroundColor White
Write-Host "  API Docs              : http://127.0.0.1:9000/docs" -ForegroundColor White
Write-Host "  Health Check          : http://127.0.0.1:9000/health" -ForegroundColor White
Write-Host "========================================================" -ForegroundColor Green
Write-Host ""
