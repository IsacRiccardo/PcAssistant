# Jarvis one-click setup: installs Python 3.11, Ollama, all packages and models.
# Safe to run again - it skips anything already installed.

$ErrorActionPreference = "Continue"   # we check exit codes ourselves; "Stop" breaks native commands on PowerShell 5.1
$ProgressPreference = "SilentlyContinue"   # makes downloads much faster
Set-Location $PSScriptRoot

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Test-Py311 {
    try { & py -3.11 --version *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

# first run: start from the factory settings (change them any time with the "Configure Jarvis" task)
foreach ($name in "config.json", "ui.json") {
    if (-not (Test-Path "config\$name")) { Copy-Item "config\defaults\$name" "config\$name" }
}
$cfg = Get-Content config\config.json -Raw -Encoding UTF8 | ConvertFrom-Json

# --- Python ---------------------------------------------------------------
Step "Checking Python 3.11"
if (-not (Test-Py311)) {
    Warn "Not found - installing with winget..."
    winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
    Refresh-Path
    if (-not (Test-Py311)) { throw "Python 3.11 still not found. Install it from python.org, restart VS Code, and rerun setup." }
}
& py -3.11 --version

# --- Ollama ---------------------------------------------------------------
Step "Checking Ollama"
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Warn "Not found - installing with winget..."
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
    Refresh-Path
    $local = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
    if (Test-Path (Join-Path $local "ollama.exe")) { $env:Path += ";$local" }
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) { throw "Ollama not found. Install it from ollama.com and rerun setup." }

# --- Virtual environment + packages ---------------------------------------
Step "Creating virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & py -3.11 -m venv .venv }
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Step "Installing Python packages"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed - see the error above." }

Step "Installing Piper TTS (natural voice)"
& $vpy -m pip install "piper-tts>=1.3.0"
if ($LASTEXITCODE -ne 0) { Warn "Piper failed to install - Jarvis will use the built-in Windows voice instead." }

# --- Models ---------------------------------------------------------------
Step "Downloading wake word models"
& $vpy -c "import openwakeword.utils as u; u.download_models()"

Step "Downloading Whisper '$($cfg.whisper_model)' speech model"
& $vpy -c "from faster_whisper import WhisperModel; WhisperModel('$($cfg.whisper_model)', device='cpu', compute_type='int8')"

New-Item -ItemType Directory -Force voices | Out-Null
foreach ($name in $cfg.piper_voices) {        # e.g. en_US-lessac-medium, ro_RO-mihai-medium
    Step "Downloading Piper voice '$name'"
    $parts = $name.Split("-")
    $locale = $parts[0]; $speaker = $parts[1]; $quality = $parts[2]
    $family = $locale.Split("_")[0]
    $base = "https://huggingface.co/rhasspy/piper-voices/resolve/main/$family/$locale/$speaker/$quality/$name"
    foreach ($ext in ".onnx", ".onnx.json") {
        $out = "voices\$name$ext"
        if (-not (Test-Path $out)) { Invoke-WebRequest "$base$ext" -OutFile $out }
    }
}

Step "Starting Ollama and pulling language models"
try { ollama list *> $null } catch {}
if ($LASTEXITCODE -ne 0) {
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 4
}
ollama pull $cfg.model_plugged_in
ollama pull $cfg.model_on_battery

Write-Host "`nAll set! In VS Code: Terminal > Run Task > 'Run Jarvis (text mode)' to test, then 'Run Jarvis (voice)'." -ForegroundColor Green
