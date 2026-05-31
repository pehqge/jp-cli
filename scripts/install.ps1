# jp installer for Windows (PowerShell).
#
# Usage:
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/pehqge/jpsync/main/scripts/install.ps1 | iex"
#
# Downloads the standalone jp.exe from the latest GitHub release and installs it
# to %LOCALAPPDATA%\jp\bin, adding that directory to the user PATH.

$ErrorActionPreference = "Stop"

$Repo   = "pehqge/jpsync"
$BinDir = Join-Path $env:LOCALAPPDATA "jp\bin"
$Asset  = "jp-windows-x86_64.exe"
$Url    = "https://github.com/$Repo/releases/latest/download/$Asset"

Write-Host "jp: downloading $Asset ..."
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

try {
    Invoke-WebRequest -Uri $Url -OutFile (Join-Path $BinDir "jp.exe")
} catch {
    Write-Error "jp: download failed from $Url. The release may not include a binary yet. Try: pipx install jpsync"
    exit 1
}

Write-Host "jp: installed to $BinDir\jp.exe"

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$BinDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$BinDir", "User")
    Write-Host "jp: added $BinDir to your user PATH. Open a new terminal for it to take effect."
}

Write-Host ""
Write-Host "jp: done. Run 'jp --help' to get started."
