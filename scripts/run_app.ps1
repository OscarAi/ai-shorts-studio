# Starts Shorts Studio and opens it in the browser. Called by run_app.bat (double-click that).
# Every step is shown in the window and written to data\logs\run_app.log.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Port = 8501
$Url = "http://localhost:$Port"
$Health = "http://127.0.0.1:$Port/_stcore/health"   # Streamlit's own "I'm ready" address
$LogDir = Join-Path $Root "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "run_app.log"

function Say($text, $color = "Gray") {
    Write-Host $text -ForegroundColor $color
    Add-Content -Path $Log -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text"
}

function AppIsReady {
    try {
        $r = Invoke-WebRequest -UseBasicParsing $Health -TimeoutSec 2
        return ($r.Content -eq "ok")
    } catch { return $false }
}

function PortIsBusy {
    $client = New-Object System.Net.Sockets.TcpClient
    try { $client.Connect("127.0.0.1", $Port); return $true } catch { return $false }
    finally { $client.Close() }
}

Add-Content -Path $Log -Value ""
Say "run_app started in $Root"

# 1. Already running? Then just open it.
if (AppIsReady) {
    Say "Shorts Studio is already running. Opening $Url" "Green"
    Start-Process $Url
    Start-Sleep -Seconds 3
    exit 0
}
if (PortIsBusy) {
    Say "Port $Port is used by another program, so the app can't start." "Red"
    Say "Close other terminals that run 'streamlit', or restart the PC, then try again." "Yellow"
    exit 1
}

# 2. The project's own Python.
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Say "The project's Python folder (.venv) is missing. Run once:" "Red"
    Say "    powershell -ExecutionPolicy Bypass -File setup.ps1" "Yellow"
    exit 1
}

# 3. Start the app in this window, wait until Streamlit says it's ready, then open the browser.
Say "Starting Shorts Studio..." "Cyan"
$app = Start-Process -FilePath $Python -NoNewWindow -PassThru -ArgumentList @(
    "-m", "streamlit", "run", "dashboard.py",
    "--server.port", "$Port", "--server.headless", "true")
$waited = 0
while (-not (AppIsReady)) {
    if ($app.HasExited) {
        Say "The app stopped while starting (exit code $($app.ExitCode)). The error is above; send it to Claude." "Red"
        exit 1
    }
    if ($waited -ge 120) {
        Say "The app did not get ready within 2 minutes. Send this window's text to Claude." "Red"
        exit 1
    }
    Start-Sleep -Seconds 1
    $waited++
}
Say "Ready after $waited s. Opening $Url in your browser." "Green"
Start-Process $Url
Write-Host ""
Write-Host "Keep this window open while you work. Close it to stop the app." -ForegroundColor Yellow
Write-Host "If no tab appeared, type $Url in your browser." -ForegroundColor Yellow

# 4. Stay here while the app runs; say so if it stops.
$app.WaitForExit()
Say "The app stopped (exit code $($app.ExitCode))." "Yellow"
exit $app.ExitCode
