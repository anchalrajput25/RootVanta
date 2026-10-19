# Install and start Rootvanta with Docker (Windows PowerShell): .\install.ps1
# Creates .env with new random secrets the first time, then starts everything.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is needed: https://docs.docker.com/get-docker/" }
function New-Secret {
    $bytes = New-Object byte[] 36
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return ([Convert]::ToBase64String($bytes) -replace '[/+=]', '')
}
if (-not (Test-Path .env)) {
    @(
        "# Made by install.ps1. Keep this file private, and back it up: without"
        "# INCIDENT_AGENT_SECRET_KEY the saved monitoring tokens cannot be read."
        "INCIDENT_AGENT_JWT_SECRET=$(New-Secret)"
        "INCIDENT_AGENT_SECRET_KEY=$(New-Secret)"
        "POSTGRES_PASSWORD=$(New-Secret)"
        "APP_DB_PASSWORD=$(New-Secret)"
        "# Set to 1 when several companies share this server (blocks private network addresses)."
        "INCIDENT_AGENT_BLOCK_PRIVATE_URLS=0"
        "WORKERS=2"
    ) | Set-Content -Encoding ascii .env
    Write-Host "Created .env with new secrets."
}
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose failed; see the messages above." }
Write-Host ""
Write-Host "Rootvanta is starting. Open http://localhost:3001 and create your company." -ForegroundColor Green
