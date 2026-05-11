param(
  [switch]$Build
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if ($Build) {
  Write-Host "[agent-build] Starting (first run: venv + deps + PyInstaller can take 5-15 min)."
  $Venv = Join-Path $Root ".venv-agent-build"
  if (-not (Test-Path $Venv)) {
    Write-Host "[agent-build] Creating venv: $Venv"
    python -m venv $Venv
  } else {
    Write-Host "[agent-build] Using existing venv: $Venv"
  }
  Write-Host "[agent-build] Activating venv..."
  & (Join-Path $Venv "Scripts\Activate.ps1")
  Write-Host "[agent-build] pip install -r requirements.txt"
  pip install -r requirements.txt
  Write-Host "[agent-build] pip install -r requirements-agent-exe.txt"
  pip install -r requirements-agent-exe.txt

  $Out = Join-Path $Root "dist"
  $SpecLauncher = Join-Path $Root "scripts\agent_windows_launcher.py"
  $TrayLauncher = Join-Path $Root "scripts\agent_windows_tray_launcher.py"

  $AgentData = "$(Join-Path $Root 'agent');agent"
  $CommonArgs = @(
    "--noconfirm", "--clean", "--onefile",
    "--distpath", $Out,
    "--workpath", (Join-Path $Root "build\pyinstaller"),
    "--specpath", (Join-Path $Root "build"),
    "--paths", $Root,
    "--add-data", $AgentData,
    "--hidden-import=yaml",
    "--hidden-import=httpx",
    "--hidden-import=httpcore",
    "--hidden-import=h11",
    "--hidden-import=certifi",
    "--hidden-import=psutil",
    "--hidden-import=dotenv",
    "--hidden-import=pystray",
    "--hidden-import=PIL",
    "--hidden-import=PIL.Image"
  )

  Write-Host "[agent-build] PyInstaller (1/2) MonitoringAgent.exe ..."
  pyinstaller @CommonArgs --name MonitoringAgent $SpecLauncher
  Write-Host "[agent-build] PyInstaller (2/2) MonitoringAgentTray.exe ..."
  pyinstaller @CommonArgs --windowed --name MonitoringAgentTray $TrayLauncher

  Write-Host "Done: $Out\MonitoringAgent.exe (console), $Out\MonitoringAgentTray.exe (tray)"
  exit 0
}

if (-not $env:SERVER_URL) { $env:SERVER_URL = "http://127.0.0.1:8000" }

if (-not $env:HOST_ID) {
  try {
    $uuid = (Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop).UUID
    $hex = ($uuid -replace "-", "").ToLower()
    $short = if ($hex.Length -ge 8) { $hex.Substring(0, 8) } else { $hex.PadRight(8, "0") }
  } catch {
    $short = [guid]::NewGuid().ToString("N").Substring(0, 8)
  }
  $hn = $env:COMPUTERNAME.ToLower()
  $env:HOST_ID = "$hn-$short"
}

if (-not $env:HOST_NAME) {
  $env:HOST_NAME = 'Windows (' + $env:COMPUTERNAME + ')'
}

if (-not $env:DISK_USAGE_PATH) { $env:DISK_USAGE_PATH = $env:SystemDrive + '\' }

python -m agent.main
