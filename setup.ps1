# One-time setup (safe to run again): creates .venv and installs the exact tested versions.
# Run in the VS Code terminal:   powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv (Python 3.13)..."
    py -3.13 -m venv .venv
}
Write-Host "Installing the tested library versions from requirements-lock.txt..."
& .venv\Scripts\python.exe -m pip install --upgrade pip --quiet
& .venv\Scripts\python.exe -m pip install -r requirements-lock.txt --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed - copy the output and send it to Claude." }

if (Test-Path ".git") {
    Write-Host "Installing the secret guard (blocks commits that contain an AI key or .env)..."
    Copy-Item "scripts\pre-commit" ".git\hooks\pre-commit" -Force
}
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example: paste your free AI key(s) into it (README: 'AI inside the app')."
}

Write-Host ""
Write-Host "Done. Start the app:   .venv\Scripts\python -m streamlit run dashboard.py"
Write-Host "Run the tests:         .venv\Scripts\python -m pytest"
Write-Host "Commands:              .venv\Scripts\python cli.py list"
