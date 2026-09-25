[CmdletBinding()]
param(
    [string]$PythonExe = '',
    [string]$ConfigPath = '',
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$BridgeArguments
)
$ErrorActionPreference = 'Stop'
$bridgeRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$launcherFile = Join-Path $bridgeRoot '.mcp.json'
$launcher = $null
if (Test-Path -LiteralPath $launcherFile -PathType Leaf) {
    $launcher = Get-Content -LiteralPath $launcherFile -Raw | ConvertFrom-Json
}
if (-not $PythonExe) {
    if ($null -ne $launcher) {
        $PythonExe = [string]$launcher.mcpServers.codex_bridge.command
    }
    if (-not $PythonExe) { $PythonExe = 'python' }
}
if (-not $ConfigPath -and -not $env:CODEX_BRIDGE_CONFIG -and $null -ne $launcher) {
    $launcherArguments = @($launcher.mcpServers.codex_bridge.args)
    $configFlagIndex = [Array]::IndexOf($launcherArguments, '--config')
    if ($configFlagIndex -ge 0 -and ($configFlagIndex + 1) -lt $launcherArguments.Count) {
        $ConfigPath = [string]$launcherArguments[$configFlagIndex + 1]
    }
}
$commandArguments = @('-I', (Join-Path $bridgeRoot 'scripts\run_bridge.py'))
if ($ConfigPath) { $commandArguments += @('--config', $ConfigPath) }
if ($BridgeArguments) { $commandArguments += $BridgeArguments }
else { $commandArguments += '--help' }
$oldPythonPath = $env:PYTHONPATH
$bridgeExitCode = 1
try {
    $env:PYTHONPATH = $bridgeRoot
    & $PythonExe @commandArguments
    $bridgeExitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $oldPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $oldPythonPath }
}
exit $bridgeExitCode
