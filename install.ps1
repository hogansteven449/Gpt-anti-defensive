[CmdletBinding()]
param(
    [string]$PythonExe,
    [string]$SkillsDir,
    [switch]$Replace,
    [switch]$DryRun,
    [switch]$WithFormats,
    [switch]$WithPandoc
)
$ErrorActionPreference = 'Stop'
$taskCandidates = @()
if ($PythonExe) {
    $taskCandidates += [pscustomobject]@{ File = $PythonExe; Prefix = @() }
} else {
    $taskLauncher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
    if ($taskLauncher) { $taskCandidates += [pscustomobject]@{ File = $taskLauncher.Source; Prefix = @('-3') } }
    $taskBundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $taskBundled -PathType Leaf) {
        $taskCandidates += [pscustomobject]@{ File = $taskBundled; Prefix = @() }
    }
    foreach ($taskCommand in @('python', 'python3')) {
        $taskFound = Get-Command $taskCommand -CommandType Application -ErrorAction SilentlyContinue
        if ($taskFound -and $taskFound.Source -notmatch '[\\/]Microsoft[\\/]WindowsApps[\\/]') {
            $taskCandidates += [pscustomobject]@{ File = $taskFound.Source; Prefix = @() }
        }
    }
}
$taskSelected = $null
foreach ($taskCandidate in $taskCandidates) {
    try {
        $taskPrefix = @($taskCandidate.Prefix)
        & $taskCandidate.File @taskPrefix -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $taskSelected = $taskCandidate; break }
    } catch { }
}
if (-not $taskSelected) { throw 'Python 3.11+ was not found. Run install.ps1 -PythonExe <path-to-python.exe>.' }
$taskInstallArguments = @((Join-Path $PSScriptRoot 'install.py'))
if ($SkillsDir) { $taskInstallArguments += @('--skills-dir', $SkillsDir) }
if ($Replace) { $taskInstallArguments += '--replace' }
if ($DryRun) { $taskInstallArguments += '--dry-run' }
if ($WithFormats) { $taskInstallArguments += '--with-formats' }
if ($WithPandoc) { $taskInstallArguments += '--with-pandoc' }
$taskPrefix = @($taskSelected.Prefix)
& $taskSelected.File @taskPrefix @taskInstallArguments
exit $LASTEXITCODE
