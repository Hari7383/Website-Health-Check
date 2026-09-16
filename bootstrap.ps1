<#
    Website Hygiene Check - install everything from scratch

    For a PC with nothing installed. Run once:

        powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1

    Installs Python if it is missing, then hands over to setup.ps1 for the
    dependencies and browser. Safe to re-run; it skips whatever is already
    present.
#>

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Write-Host ''
Write-Host 'Website Hygiene Check - full install' -ForegroundColor Cyan
Write-Host '====================================' -ForegroundColor Cyan
Write-Host ''

function Get-WorkingPython {
    foreach ($candidate in @('python', 'py')) {
        try {
            $version = & $candidate --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $version -match 'Python (\d+)\.(\d+)') {
                if ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10) {
                    return [pscustomobject]@{ Command = $candidate; Version = "$version" }
                }
            }
        } catch { }
    }
    return $null
}

function Update-SessionPath {
    # winget updates the machine/user PATH, but not the PATH of this already
    # running shell. Re-read both so python is found without reopening.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# --- Step 1: Python ---------------------------------------------------------
Write-Host '[1/3] Checking for Python 3.10 or newer ...'
$python = Get-WorkingPython

if ($python) {
    Write-Host "      Already installed: $($python.Version)" -ForegroundColor Green
} else {
    Write-Host '      Not found. Installing Python 3.12 ...' -ForegroundColor Yellow

    $haveWinget = $false
    try { winget --version *> $null; $haveWinget = ($LASTEXITCODE -eq 0) } catch { }

    if (-not $haveWinget) {
        Write-Host ''
        Write-Host '      winget is not available on this PC.' -ForegroundColor Red
        Write-Host '      Install Python manually instead:'
        Write-Host '        1. Open https://www.python.org/downloads/'
        Write-Host '        2. Download Python 3.12 for Windows'
        Write-Host '        3. Run the installer and TICK "Add python.exe to PATH"'
        Write-Host '        4. Open a new PowerShell window and run this script again'
        Write-Host ''
        exit 1
    }

    winget install --id Python.Python.3.12 --source winget `
        --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host '      The Python install did not complete.' -ForegroundColor Red
        Write-Host '      Install it manually from https://www.python.org/downloads/'
        Write-Host '      and be sure to tick "Add python.exe to PATH".'
        Write-Host ''
        exit 1
    }

    Update-SessionPath
    $python = Get-WorkingPython

    if (-not $python) {
        Write-Host ''
        Write-Host '      Python installed, but this window cannot see it yet.' -ForegroundColor Yellow
        Write-Host '      Close this window, open a NEW PowerShell, and run:'
        Write-Host '          powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1'
        Write-Host ''
        exit 1
    }
    Write-Host "      Installed: $($python.Version)" -ForegroundColor Green
}

# --- Step 2: dependencies and browser --------------------------------------
Write-Host ''
Write-Host '[2/3] Installing dependencies and browser ...'
Write-Host ''

& powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'setup.ps1')
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Setup failed. See the messages above.' -ForegroundColor Red
    exit 1
}

# --- Step 3: workbook -------------------------------------------------------
Write-Host '[3/3] Looking for the checklist workbook ...'
$found = @()
foreach ($folder in @($PSScriptRoot,
                      (Join-Path $HOME 'Downloads'),
                      (Join-Path $HOME 'Desktop'),
                      (Join-Path $HOME 'Documents'))) {
    if (Test-Path $folder) {
        $found += Get-ChildItem -Path $folder -Filter '*Hygiene*Checklist*.xls*' -ErrorAction SilentlyContinue
    }
}

if ($found.Count -gt 0) {
    Write-Host "      Found: $($found[0].FullName)" -ForegroundColor Green
} else {
    Write-Host '      No checklist workbook found yet.' -ForegroundColor Yellow
    Write-Host '      Copy the Excel file into this folder, Downloads, Desktop or'
    Write-Host '      Documents, or set the path on the dashboard once running.'
}

Write-Host ''
Write-Host 'Everything is installed.' -ForegroundColor Green
Write-Host ''
Write-Host 'Start the tool with:' -ForegroundColor Cyan
Write-Host '    powershell -ExecutionPolicy Bypass -File .\run.ps1'
Write-Host ''
