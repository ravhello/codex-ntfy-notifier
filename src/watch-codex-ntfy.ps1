[CmdletBinding()]
param(
  [int]$PollSeconds = 2
)

$ErrorActionPreference = 'Stop'
$notifier = Join-Path $PSScriptRoot 'notify-ntfy.ps1'

if (-not (Test-Path -LiteralPath $notifier)) {
  exit 2
}

$windowsPowerShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$restartDelay = [Math]::Max(1, $PollSeconds)
while ($true) {
  try {
    & $windowsPowerShell `
      -NoProfile `
      -NonInteractive `
      -ExecutionPolicy Bypass `
      -File $notifier `
      -Worker `
      -Continuous `
      -PollSeconds $PollSeconds
  } catch {
    # The scheduled task remains the long-lived supervisor and retries below.
  }
  Start-Sleep -Seconds $restartDelay
}
