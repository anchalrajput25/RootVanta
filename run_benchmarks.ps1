# Runs the step-8 benchmarks on this PC and saves the results in the "results" folder.
#   1. Practice incidents (1,000 generated incidents, about 1 minute, no download)
#   2. RCAEval, the public benchmark of real microservice failures (downloads the data first)
# Double-click run_benchmarks.bat to start it (run_benchmarks_all.bat runs every suite without asking).
param([string]$Choice = "")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = $null
foreach ($p in @("..\.venv\Scripts\python.exe", ".venv\Scripts\python.exe")) {
    if (Test-Path $p) { $python = (Resolve-Path $p).Path; break }
}
if (-not $python) { $python = "python" }
New-Item -ItemType Directory -Force -Path results | Out-Null

Write-Host ""
Write-Host "Step 1 of 3: installing what the benchmarks need..." -ForegroundColor Cyan
& $python -m pip install --quiet -e ".[bench]"
if ($LASTEXITCODE -ne 0) { Write-Host "Install failed. See the message above." -ForegroundColor Red; Read-Host "Press Enter to close"; exit 1 }

Write-Host ""
Write-Host "Step 2 of 3: practice incidents (about a minute)..." -ForegroundColor Cyan
& $python -m incident_agent.evals.practice --cases 1000 --seed 2026 --json results\practice.json | Tee-Object -FilePath results\practice.txt

Write-Host ""
Write-Host "Step 3 of 3: RCAEval public benchmark" -ForegroundColor Cyan
Write-Host "  1 = RE1 only: 375 cases, metrics only (smaller download)"
Write-Host "  2 = RE1, RE2 and RE3: all 735 cases with logs and traces (about 3.4 GB download)"
$choice = $Choice
if (-not $choice) { $choice = Read-Host "Type 1 or 2 and press Enter (default 1)" }
$suites = @("re1")
if ($choice -eq "2") { $suites = @("re1", "re2", "re3") }
$cmd = @("-m", "incident_agent.evals.rcaeval", "--data", "data\rcaeval", "--json", "results\rcaeval.json")
foreach ($s in $suites) {
    if (-not (Test-Path "data\rcaeval\$s*")) { $cmd += @("--download", $s) }
}
if ($choice -ne "2") { $cmd += @("--suite", "RE1") }
& $python @cmd | Tee-Object -FilePath results\rcaeval.txt

Write-Host ""
Write-Host "Done. Results are in the results folder (practice.txt, rcaeval.txt)." -ForegroundColor Green
Read-Host "Press Enter to close"
