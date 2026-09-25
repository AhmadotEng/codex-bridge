[CmdletBinding()]
param(
    [string]$PythonExe = 'python',
    [string]$InstallDirectory = (Join-Path $env:USERPROFILE 'plugins\codex-bridge'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex-bridge\config.json'),
    [switch]$Upgrade
)
$ErrorActionPreference = 'Stop'
$pythonCommand = Get-Command $PythonExe -CommandType Application -ErrorAction Stop | Select-Object -First 1
$arguments = @('-I', (Join-Path $PSScriptRoot 'install.py'), '--directory', $InstallDirectory, '--config', $ConfigPath)
if ($Upgrade) { $arguments += '--upgrade' }
$result = & $pythonCommand.Source @arguments
if ($LASTEXITCODE -ne 0) { throw "Connector installation failed: $result" }
$result
