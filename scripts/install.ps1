[CmdletBinding()]
param(
    [string]$PythonExe = 'python',
    [string]$InstallDirectory = (Join-Path $env:USERPROFILE 'plugins\codex-bridge'),
    [string]$ConfigPath = (Join-Path $env:USERPROFILE '.codex-bridge\config.json')
)
$ErrorActionPreference = 'Stop'
$sourceDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$targetDirectory = [IO.Path]::GetFullPath($InstallDirectory)
$resolvedConfig = [IO.Path]::GetFullPath($ConfigPath)
function Assert-BridgeDestinationHasNoLinks {
    param([string]$CandidatePath)
    $checkedPath = [IO.Path]::GetFullPath($CandidatePath)
    while ($checkedPath) {
        # Get-Item also sees an existing link whose destination is missing.
        $checkedItem = Get-Item -LiteralPath $checkedPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $checkedItem -and ($checkedItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'The selected install destination must not contain linked files or directories.'
        }
        $parentPath = [IO.Path]::GetDirectoryName($checkedPath.TrimEnd('\', '/'))
        if (-not $parentPath -or $parentPath -eq $checkedPath) { break }
        $checkedPath = $parentPath
    }
}
if ((Split-Path -Leaf $targetDirectory.TrimEnd('\')) -ne 'codex-bridge') {
    throw 'The install directory must be named codex-bridge to match the plugin manifest.'
}
if ($targetDirectory.StartsWith($sourceDirectory.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The install directory cannot be inside the source bundle.'
}
$pythonCommand = Get-Command $PythonExe -CommandType Application -ErrorAction Stop | Select-Object -First 1
$resolvedPython = $pythonCommand.Source
$pythonInfoText = & $resolvedPython -c 'import json,sys; print(json.dumps(dict(executable=sys.executable,version=list(sys.version_info[:3]))))'
if ($LASTEXITCODE -ne 0) { throw 'Python could not be started.' }
$pythonInfo = $pythonInfoText | ConvertFrom-Json
if ($pythonInfo.version[0] -ne 3 -or $pythonInfo.version[1] -lt 11) {
    throw 'Codex Bridge requires Python 3.11 or newer.'
}
$resolvedPython = [string]$pythonInfo.executable
# Explicit files keep Git metadata, local configuration, logs, caches, and
# unrelated untracked files out of the installed plugin. Add new runtime
# modules here deliberately; never replace this with a recursive copy.
$requiredFiles = @(
    '.codex-plugin\plugin.json',
    'codex_bridge\__init__.py',
    'codex_bridge\artifacts.py',
    'codex_bridge\cli.py',
    'codex_bridge\codex_adapter.py',
    'codex_bridge\compatibility.py',
    'codex_bridge\core.py',
    'codex_bridge\local_actions.py',
    'codex_bridge\mcp.py',
    'codex_bridge\onboarding.py',
    'codex_bridge\tools.py',
    'codex_bridge\transport.py',
    'scripts\bridge.ps1',
    'scripts\install.ps1',
    'scripts\setup.ps1',
    'scripts\bridge.sh',
    'scripts\install.py',
    'scripts\install.sh',
    'skills\collaborate\SKILL.md'
)
$optionalFiles = @(
    'README.md', 'LICENSE', 'LICENSE.md', 'NOTICES', 'NOTICES.md',
    'config.example.json',
    'docs\SETUP.md', 'docs\ONBOARDING-PLAN.md', 'docs\ADVANCED.md',
    'docs\SECURITY.md', 'docs\TESTING.md',
    'examples\computer-a.example.json', 'examples\computer-b.example.json'
)
$selectedFiles = @($requiredFiles)
foreach ($relativePath in $optionalFiles) {
    if (Test-Path -LiteralPath (Join-Path $sourceDirectory $relativePath) -PathType Leaf) {
        $selectedFiles += $relativePath
    }
}
# Validate the complete selection before copying any file. Reparse points
# could otherwise make a named source file read outside this source tree.
foreach ($relativePath in $selectedFiles) {
    $sourcePath = Join-Path $sourceDirectory $relativePath
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Required plugin file is missing: $relativePath"
    }
    $sourceItem = Get-Item -LiteralPath $sourcePath -Force
    while ($sourceItem.FullName.TrimEnd('\') -ne $sourceDirectory.TrimEnd('\')) {
        if (($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Plugin source must not contain linked files or directories: $relativePath"
        }
        $parentPath = Split-Path -Parent $sourceItem.FullName
        if (-not $parentPath) {
            throw "Plugin source path escaped the selected source directory: $relativePath"
        }
        $sourceItem = Get-Item -LiteralPath $parentPath -Force
    }
}
# Check every destination before the first write, including same-directory
# refreshes and the generated launcher. Existing links can redirect a copy
# or WriteAllText into unrelated owner files outside this installation.
foreach ($relativePath in $selectedFiles) {
    Assert-BridgeDestinationHasNoLinks (Join-Path $targetDirectory $relativePath)
}
Assert-BridgeDestinationHasNoLinks (Join-Path $targetDirectory '.mcp.json')
if ($sourceDirectory.TrimEnd('\') -ne $targetDirectory.TrimEnd('\')) {
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    foreach ($relativePath in $selectedFiles) {
        $targetPath = Join-Path $targetDirectory $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $targetPath) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $sourceDirectory $relativePath) -Destination $targetPath -Force
    }
}
$mcp = @{ mcpServers = @{ codex_bridge = @{
    command = $resolvedPython
    args = @('-u', (Join-Path $targetDirectory 'codex_bridge\mcp.py'), '--config', $resolvedConfig)
} } }
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $targetDirectory '.mcp.json'), ($mcp | ConvertTo-Json -Depth 8), $utf8)
[ordered]@{
    ok = $true
    installed_directory = $targetDirectory
    python = $resolvedPython
    config_path = $resolvedConfig
    config_exists = (Test-Path -LiteralPath $resolvedConfig -PathType Leaf)
    registration_required = $true
    note = 'Connector files and local MCP launcher are ready. Register the MCP server or activate the plugin; configuration and pairing are separate. Existing credentials and settings were not changed.'
} | ConvertTo-Json -Depth 5
