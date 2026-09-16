<#
    Website Hygiene Check - start the tool

        powershell -ExecutionPolicy Bypass -File .\run.ps1

    Leave this window open while you use the tool. Closing it, or pressing
    Ctrl+C, stops the server.
#>

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Host ''
    Write-Host 'The environment is missing. Run setup first:' -ForegroundColor Yellow
    Write-Host '    powershell -ExecutionPolicy Bypass -File .\setup.ps1'
    Write-Host ''
    exit 1
}

Write-Host ''
Write-Host 'Website Hygiene Check' -ForegroundColor Cyan
Write-Host '  http://127.0.0.1:5000' -ForegroundColor Cyan
Write-Host ''
Write-Host '  Keep this window open. Ctrl+C stops the server.'
Write-Host ''

Start-Process 'http://127.0.0.1:5000'
& $venvPython manage.py runserver
