[CmdletBinding()]
param(
    [string]$PythonExe = '',
    [string]$CodexExe = '',
    [string]$PeerId = '',
    [string]$InstallDirectory = (Join-Path $env:USERPROFILE 'plugins\codex-bridge'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex-bridge\config.json'),
    [switch]$Batch,
    [switch]$SkipRegistration
)
$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $PythonExe) {
    $pythonCommand = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pythonCommand) { $PythonExe = $pythonCommand.Source }
    if (-not $PythonExe) {
        $pyCommand = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pyCommand) {
            $detectedPython = & $pyCommand.Source -3 -c 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0) { $PythonExe = [string]$detectedPython }
        }
    }
    if (-not $PythonExe) {
        throw 'Python 3.11+ was not found. Install it locally, or run setup.ps1 -PythonExe with its full executable path.'
    }
}
$installationText = & (Join-Path $sourceRoot 'scripts\install.ps1') -PythonExe $PythonExe -InstallDirectory $InstallDirectory -ConfigPath $ConfigPath
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'Connector installation failed.' }
$installation = $installationText | ConvertFrom-Json
$arguments = @('-m', 'codex_bridge.cli', '--config', [string]$installation.config_path, 'setup')
if ($CodexExe) { $arguments += @('--codex', $CodexExe) }
if ($PeerId) { $arguments += @('--peer-id', $PeerId) }
if ($Batch) { $arguments += '--batch' }
else { $arguments += '--guided' }
if (-not $SkipRegistration) { $arguments += '--register-mcp' }
$oldPythonPath = $env:PYTHONPATH
$setupExitCode = 1
try {
    $env:PYTHONPATH = [string]$installation.installed_directory
    & ([string]$installation.python) @arguments
    $setupExitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $oldPythonPath }
}
exit $setupExitCode
