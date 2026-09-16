<#
    Website Hygiene Check - first-time setup

    Run once on a new machine:
        powershell -ExecutionPolicy Bypass -File .\setup.ps1

    Creates an isolated Python environment in .venv, installs the four
    dependencies, and downloads the Chromium build Playwright drives. Nothing
    is installed system-wide beyond Python itself.
#>

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Write-Host ''
Write-Host 'Website Hygiene Check - setup' -ForegroundColor Cyan
Write-Host '=============================' -ForegroundColor Cyan
Write-Host ''

# --- 1. Find a usable Python ------------------------------------------------
$python = $null
foreach ($candidate in @('python', 'py')) {
    try {
        $version = & $candidate --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $version -match 'Python (\d+)\.(\d+)') {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 10) {
                $python = $candidate
                Write-Host "Found $version" -ForegroundColor Green
                break
            }
            Write-Host "$version is too old - 3.10 or newer is required." -ForegroundColor Yellow
        }
    } catch { }
}

if (-not $python) {
    Write-Host ''
    Write-Host 'Python 3.10 or newer was not found.' -ForegroundColor Red
    Write-Host 'Install it from https://www.python.org/downloads/'
    Write-Host 'On the first installer screen, tick "Add python.exe to PATH".'
    Write-Host 'Then close this window, open a new PowerShell, and run setup again.'
    exit 1
}

# --- 2. Virtual environment -------------------------------------------------
if (Test-Path '.venv') {
    Write-Host 'Using the existing .venv folder.'
} else {
    Write-Host 'Creating an isolated environment in .venv ...'
    & $python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Host 'Could not create .venv' -ForegroundColor Red; exit 1 }
}

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Host "Expected $venvPython but it is missing." -ForegroundColor Red
    exit 1
}

# --- 3. Dependencies --------------------------------------------------------
Write-Host ''
Write-Host 'Installing dependencies (Flask, requests, openpyxl, playwright) ...'
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host 'Dependency install failed.' -ForegroundColor Red; exit 1 }
Write-Host 'Dependencies installed.' -ForegroundColor Green

# --- 4. Browser -------------------------------------------------------------
Write-Host ''
Write-Host 'Downloading Chromium for Playwright (about 150 MB, one time) ...'
& $venvPython -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Chromium download failed. Check the network and re-run setup.' -ForegroundColor Red
    exit 1
}

# --- 5. Verify --------------------------------------------------------------
Write-Host ''
Write-Host 'Verifying the browser starts ...'
& $venvPython -c @"
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); b.close()
print('browser OK')
"@
if ($LASTEXITCODE -ne 0) { Write-Host 'Browser verification failed.' -ForegroundColor Red; exit 1 }

Write-Host ''
Write-Host 'Setup complete.' -ForegroundColor Green
Write-Host ''
Write-Host 'Start the tool with:' -ForegroundColor Cyan
Write-Host '    .\run.ps1'
Write-Host ''
Write-Host 'Then open http://127.0.0.1:5000 and set the workbook path on the dashboard.'
Write-Host ''
