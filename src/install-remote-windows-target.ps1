[CmdletBinding()]
param(
  [string]$Origin = 'SSH:Windows',
  [switch]$SkipScheduledTask
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$HomePath = $PSScriptRoot
$ConfigPath = Join-Path $HomePath 'config.toml'
$HooksPath = Join-Path $HomePath 'hooks.json'
$ScriptPath = Join-Path $HomePath 'notify-ntfy.ps1'
$WatcherPath = Join-Path $HomePath 'watch-codex-ntfy.ps1'
$HiddenWatcherPath = Join-Path $HomePath 'watch-codex-ntfy-hidden.vbs'
$PrivateConfig = Join-Path $HomePath 'ntfy-config.json'
$StatePath = Join-Path $HomePath 'ntfy-state'
$TaskName = 'CodexNtfyWatcher'
$Utf8StrictNoBom = New-Object System.Text.UTF8Encoding($false, $true)

function Test-SafeUnicodeScalarText {
  param([AllowNull()][AllowEmptyString()][string]$Value)
  if ($null -eq $Value) { return $true }
  for ($index = 0; $index -lt $Value.Length; $index++) {
    $unit = [int][char]$Value[$index]
    if ($unit -eq 0xFFFD) { return $false }
    if ($unit -ge 0xD800 -and $unit -le 0xDBFF) {
      if ($index + 1 -ge $Value.Length) { return $false }
      $low = [int][char]$Value[$index + 1]
      if ($low -lt 0xDC00 -or $low -gt 0xDFFF) { return $false }
      $index++
    } elseif ($unit -ge 0xDC00 -and $unit -le 0xDFFF) {
      return $false
    }
  }
  return $true
}

function Assert-SafeUnicodeScalarText {
  param([AllowNull()][AllowEmptyString()][string]$Value, [string]$Context = 'text')
  if (-not (Test-SafeUnicodeScalarText -Value $Value)) {
    throw "$Context contains invalid Unicode scalar data"
  }
}

function Assert-JsonUnicodeScalars {
  param([AllowNull()][object]$Value, [int]$Depth = 0)
  if ($Depth -gt 64) { throw 'JSON nesting exceeds the supported depth' }
  if ($null -eq $Value -or $Value -is [ValueType]) { return }
  if ($Value -is [string]) {
    Assert-SafeUnicodeScalarText -Value ([string]$Value) -Context 'JSON string'
    return
  }
  if ($Value -is [Collections.IDictionary]) {
    foreach ($key in $Value.Keys) {
      if ($key -is [string]) {
        Assert-SafeUnicodeScalarText -Value ([string]$key) -Context 'JSON object key'
      }
      Assert-JsonUnicodeScalars -Value $Value[$key] -Depth ($Depth + 1)
    }
    return
  }
  if ($Value -is [Collections.IEnumerable] -and $Value -isnot [string]) {
    foreach ($item in $Value) {
      Assert-JsonUnicodeScalars -Value $item -Depth ($Depth + 1)
    }
    return
  }
  foreach ($property in @($Value.PSObject.Properties | Where-Object { $_.MemberType -in @('NoteProperty', 'Property') })) {
    Assert-SafeUnicodeScalarText -Value ([string]$property.Name) -Context 'JSON object key'
    Assert-JsonUnicodeScalars -Value $property.Value -Depth ($Depth + 1)
  }
}

function ConvertFrom-StrictJsonText {
  param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
  Assert-SafeUnicodeScalarText -Value $Text -Context 'JSON input'
  $value = $Text | ConvertFrom-Json -ErrorAction Stop
  Assert-JsonUnicodeScalars -Value $value
  # Windows PowerShell enumerates function output.  Without validating the
  # root here, a singleton JSON array is scalarized into one PSCustomObject and
  # can incorrectly pass the object checks performed by callers.
  if ($null -eq $value -or $value -isnot [System.Management.Automation.PSCustomObject]) {
    throw 'JSON root must contain an object.'
  }
  return $value
}

function ConvertFrom-StrictUtf8Bytes {
  param(
    [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes,
    [string]$Context = 'Managed text'
  )

  $offset = 0
  if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
    $offset = 3
  }
  $text = $Utf8StrictNoBom.GetString($Bytes, $offset, $Bytes.Length - $offset)
  Assert-SafeUnicodeScalarText -Value $text -Context $Context
  return $text
}

function Read-StrictUtf8Text {
  param([Parameter(Mandatory = $true)][string]$Path, [int64]$MaxBytes = 0)
  $sharing = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
  $stream = $null
  try {
    $stream = [IO.FileStream]::new(
      $Path,
      [IO.FileMode]::Open,
      [IO.FileAccess]::Read,
      $sharing
    )
    if (($MaxBytes -gt 0 -and $stream.Length -gt $MaxBytes) -or $stream.Length -gt [int]::MaxValue) {
      throw "Managed text file exceeds the supported byte limit: $Path"
    }
    $bytes = New-Object byte[] ([int]$stream.Length)
    $read = 0
    while ($read -lt $bytes.Length) {
      $count = $stream.Read($bytes, $read, $bytes.Length - $read)
      if ($count -le 0) { throw "Managed text file ended unexpectedly: $Path" }
      $read += $count
    }
    return ConvertFrom-StrictUtf8Bytes -Bytes $bytes -Context "Managed text file $Path"
  } finally {
    if ($null -ne $stream) { $stream.Dispose() }
  }
}

function Read-StrictJsonFile {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [switch]$AllowEmpty,
    [int64]$MaxBytes = 0
  )
  $text = Read-StrictUtf8Text -Path $Path -MaxBytes $MaxBytes
  if ($AllowEmpty -and [string]::IsNullOrWhiteSpace($text)) {
    return [pscustomobject]@{}
  }
  return ConvertFrom-StrictJsonText -Text $text
}

function Protect-PrivatePath {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) { return }
  $item = Get-Item -LiteralPath $Path
  try {
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $allowedSids = @(
      $currentSid,
      (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')),
      (New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544'))
    )
    if ($item.PSIsContainer) {
      $security = New-Object System.Security.AccessControl.DirectorySecurity
      $inheritance = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
        [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
      foreach ($sid in $allowedSids) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
          $sid,
          [System.Security.AccessControl.FileSystemRights]::FullControl,
          $inheritance,
          [System.Security.AccessControl.PropagationFlags]::None,
          [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$security.AddAccessRule($rule)
      }
      $security.SetAccessRuleProtection($true, $false)
      $security.SetOwner($currentSid)
      [System.IO.Directory]::SetAccessControl($Path, $security)
    } else {
      $security = New-Object System.Security.AccessControl.FileSecurity
      foreach ($sid in $allowedSids) {
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
          $sid,
          [System.Security.AccessControl.FileSystemRights]::FullControl,
          [System.Security.AccessControl.AccessControlType]::Allow
        )
        [void]$security.AddAccessRule($rule)
      }
      $security.SetAccessRuleProtection($true, $false)
      $security.SetOwner($currentSid)
      [System.IO.File]::SetAccessControl($Path, $security)
    }
  } catch {
    throw "Could not tighten ACL for ${Path}: $($_.Exception.Message)"
  }
}

function Get-PathAclSddl {
  param([Parameter(Mandatory = $true)][string]$Path)

  $sections = [System.Security.AccessControl.AccessControlSections]::Access -bor
    [System.Security.AccessControl.AccessControlSections]::Owner -bor
    [System.Security.AccessControl.AccessControlSections]::Group
  $item = Get-Item -LiteralPath $Path -Force
  $security = if ($item.PSIsContainer) {
    [System.IO.Directory]::GetAccessControl($Path, $sections)
  } else {
    [System.IO.File]::GetAccessControl($Path, $sections)
  }
  return $security.GetSecurityDescriptorSddlForm($sections)
}

function Set-PathAclSddl {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Sddl
  )

  $sections = [System.Security.AccessControl.AccessControlSections]::Access -bor
    [System.Security.AccessControl.AccessControlSections]::Owner -bor
    [System.Security.AccessControl.AccessControlSections]::Group
  $item = Get-Item -LiteralPath $Path -Force
  if ($item.PSIsContainer) {
    $security = New-Object System.Security.AccessControl.DirectorySecurity
    $security.SetSecurityDescriptorSddlForm($Sddl, $sections)
    [System.IO.Directory]::SetAccessControl($Path, $security)
  } else {
    $security = New-Object System.Security.AccessControl.FileSecurity
    $security.SetSecurityDescriptorSddlForm($Sddl, $sections)
    [System.IO.File]::SetAccessControl($Path, $security)
  }
}

function Test-ByteArraysEqual {
  param([AllowNull()][byte[]]$Left, [AllowNull()][byte[]]$Right)

  if ($null -eq $Left -or $null -eq $Right) { return $null -eq $Left -and $null -eq $Right }
  if ($Left.Length -ne $Right.Length) { return $false }
  for ($index = 0; $index -lt $Left.Length; $index++) {
    if ($Left[$index] -ne $Right[$index]) { return $false }
  }
  return $true
}

function Read-LockedStreamBytes {
  param([Parameter(Mandatory = $true)][System.IO.Stream]$Stream)

  if ($Stream.Length -gt [int]::MaxValue) { throw 'Managed file exceeds the supported byte limit.' }
  $Stream.Position = 0
  $bytes = New-Object byte[] ([int]$Stream.Length)
  $read = 0
  while ($read -lt $bytes.Length) {
    $count = $Stream.Read($bytes, $read, $bytes.Length - $read)
    if ($count -le 0) { throw 'Managed file ended unexpectedly.' }
    $read += $count
  }
  return ,$bytes
}

function Write-BytesAtomic {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][byte[]]$Content,
    [Parameter(Mandatory = $true)][bool]$ExpectedExists,
    [AllowNull()][byte[]]$ExpectedBytes,
    [AllowNull()][string]$ExpectedAclSddl
  )

  $directory = Split-Path -Parent $Path
  if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
    throw "Atomic destination directory is missing: $directory"
  }
  if ((Test-Path -LiteralPath $Path) -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Atomic destination is not a file: $Path"
  }

  $destinationExists = Test-Path -LiteralPath $Path -PathType Leaf
  if ($destinationExists -ne $ExpectedExists) { throw "Atomic destination existence changed before replacement: $Path" }
  $temp = Join-Path $directory ('.{0}.{1}.tmp' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
  $backup = Join-Path $directory ('.{0}.{1}.rollback' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
  $preserveBackup = $false
  $destinationLock = $null
  try {
    # The empty staging file receives its final private ACL before any content.
    $empty = [System.IO.File]::Open(
      $temp,
      [System.IO.FileMode]::CreateNew,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    $empty.Dispose()
    # Never copy a possibly hostile destination ACL onto a staging file.  The
    # allowlist is installed while the file is empty, before any secret bytes.
    Protect-PrivatePath -Path $temp

    $stream = [System.IO.File]::Open(
      $temp,
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    try {
      $stream.SetLength(0)
      $stream.Write($Content, 0, $Content.Length)
      $stream.Flush($true)
    } finally {
      $stream.Dispose()
    }

    if ($destinationExists) {
      # Keep writers out between the comparison and same-volume replacement.
      $destinationLock = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
      )
      [byte[]]$lockedBytes = Read-LockedStreamBytes -Stream $destinationLock
      if (-not (Test-ByteArraysEqual -Left $lockedBytes -Right $ExpectedBytes)) {
        throw "Atomic destination content changed before replacement: $Path"
      }
      if (-not [string]::Equals((Get-PathAclSddl -Path $Path), $ExpectedAclSddl, [StringComparison]::Ordinal)) {
        throw "Atomic destination ACL changed before replacement: $Path"
      }
    }

    if ($destinationExists) {
      [System.IO.File]::Replace($temp, $Path, $backup, $true)
    } else {
      [System.IO.File]::Move($temp, $Path)
    }
  } catch {
    $writeError = $_
    if ($destinationExists -and (Test-Path -LiteralPath $backup -PathType Leaf)) {
      try {
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
          $failed = Join-Path $directory ('.{0}.{1}.failed' -f (Split-Path -Leaf $Path), [Guid]::NewGuid().ToString('N'))
          try {
            [System.IO.File]::Replace($backup, $Path, $failed, $true)
          } finally {
            if (Test-Path -LiteralPath $failed) {
              Remove-Item -LiteralPath $failed -Force -ErrorAction SilentlyContinue
            }
          }
        } else {
          [System.IO.File]::Move($backup, $Path)
        }
      } catch {
        $preserveBackup = $true
        throw "Atomic write failed for ${Path}, and rollback also failed. The prior private file remains at ${backup}. $($writeError.Exception.Message) Rollback: $($_.Exception.Message)"
      }
    }
    throw $writeError
  } finally {
    if ($null -ne $destinationLock) { $destinationLock.Dispose() }
    if (Test-Path -LiteralPath $temp) {
      Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    }
    if (-not $preserveBackup -and (Test-Path -LiteralPath $backup)) {
      Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
    }
  }
}

function Write-TextAtomic {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$Content,
    [Parameter(Mandatory = $true)][bool]$ExpectedExists,
    [AllowNull()][byte[]]$ExpectedBytes,
    [AllowNull()][string]$ExpectedAclSddl
  )

  Assert-SafeUnicodeScalarText -Value $Content -Context 'Atomic text content'
  # Strictly encode before creating a staging file.
  [byte[]]$bytes = $Utf8StrictNoBom.GetBytes([string]$Content)
  Write-BytesAtomic -Path $Path -Content $bytes -ExpectedExists $ExpectedExists -ExpectedBytes $ExpectedBytes -ExpectedAclSddl $ExpectedAclSddl
}

function New-FileTransactionRecord {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [int64]$MaxBytes = 16777216
  )

  if ((Test-Path -LiteralPath $Path) -and -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Managed transaction destination is not a file: $Path"
  }
  $exists = Test-Path -LiteralPath $Path -PathType Leaf
  [byte[]]$bytes = if ($exists) { [System.IO.File]::ReadAllBytes($Path) } else { $null }
  if ($exists -and $MaxBytes -gt 0 -and $bytes.Length -gt $MaxBytes) {
    throw "Managed transaction file exceeds the supported byte limit: $Path"
  }
  $acl = if ($exists) { Get-PathAclSddl -Path $Path } else { $null }
  return [pscustomobject]@{
    Path = [System.IO.Path]::GetFullPath($Path)
    OriginalExists = [bool]$exists
    OriginalBytes = $bytes
    OriginalAclSddl = $acl
    Mutated = $false
    InstalledExists = [bool]$exists
    InstalledBytes = $bytes
    InstalledAclSddl = $acl
  }
}

function Assert-FileTransactionState {
  param([Parameter(Mandatory = $true)][object]$Record)

  $exists = Test-Path -LiteralPath $Record.Path -PathType Leaf
  if ($exists -ne [bool]$Record.InstalledExists) {
    throw "Managed transaction destination existence changed concurrently: $($Record.Path)"
  }
  if (-not $exists) { return }
  [byte[]]$bytes = [System.IO.File]::ReadAllBytes($Record.Path)
  if (-not (Test-ByteArraysEqual -Left $bytes -Right ([byte[]]$Record.InstalledBytes))) {
    throw "Managed transaction destination content changed concurrently: $($Record.Path)"
  }
  if (-not [string]::Equals((Get-PathAclSddl -Path $Record.Path), [string]$Record.InstalledAclSddl, [StringComparison]::Ordinal)) {
    throw "Managed transaction destination ACL changed concurrently: $($Record.Path)"
  }
}

function Update-FileTransactionState {
  param([Parameter(Mandatory = $true)][object]$Record)

  $exists = Test-Path -LiteralPath $Record.Path -PathType Leaf
  $Record.InstalledExists = [bool]$exists
  $Record.InstalledBytes = if ($exists) { [byte[]][System.IO.File]::ReadAllBytes($Record.Path) } else { $null }
  $Record.InstalledAclSddl = if ($exists) { Get-PathAclSddl -Path $Record.Path } else { $null }
  $Record.Mutated = $true
}

function Set-ManagedFileBytes {
  param(
    [Parameter(Mandatory = $true)][object]$Record,
    [Parameter(Mandatory = $true)][byte[]]$Content
  )

  Assert-FileTransactionState -Record $Record
  if ($Record.InstalledExists) {
    Protect-PrivatePath -Path $Record.Path
    Update-FileTransactionState -Record $Record
  }
  if (-not $Record.InstalledExists -or
      -not (Test-ByteArraysEqual -Left ([byte[]]$Record.InstalledBytes) -Right $Content)) {
    Write-BytesAtomic `
      -Path $Record.Path `
      -Content $Content `
      -ExpectedExists ([bool]$Record.InstalledExists) `
      -ExpectedBytes ([byte[]]$Record.InstalledBytes) `
      -ExpectedAclSddl ([string]$Record.InstalledAclSddl)
    Update-FileTransactionState -Record $Record
  }
  Protect-PrivatePath -Path $Record.Path
  Update-FileTransactionState -Record $Record
}

function Set-ManagedFileText {
  param(
    [Parameter(Mandatory = $true)][object]$Record,
    [Parameter(Mandatory = $true)][string]$Content
  )

  Assert-SafeUnicodeScalarText -Value $Content -Context 'Managed transaction text'
  [byte[]]$bytes = $Utf8StrictNoBom.GetBytes($Content)
  Set-ManagedFileBytes -Record $Record -Content $bytes
}

function Restore-FileTransactionRecord {
  param([Parameter(Mandatory = $true)][object]$Record)

  if (-not [bool]$Record.Mutated) { return }
  Assert-FileTransactionState -Record $Record
  if ([bool]$Record.OriginalExists) {
    Write-BytesAtomic `
      -Path $Record.Path `
      -Content ([byte[]]$Record.OriginalBytes) `
      -ExpectedExists $true `
      -ExpectedBytes ([byte[]]$Record.InstalledBytes) `
      -ExpectedAclSddl ([string]$Record.InstalledAclSddl)
    [byte[]]$restoredBytes = [System.IO.File]::ReadAllBytes($Record.Path)
    if (-not (Test-ByteArraysEqual -Left $restoredBytes -Right ([byte[]]$Record.OriginalBytes))) {
      throw "Rollback content verification failed: $($Record.Path)"
    }
    Set-PathAclSddl -Path $Record.Path -Sddl ([string]$Record.OriginalAclSddl)
    if (-not [string]::Equals((Get-PathAclSddl -Path $Record.Path), [string]$Record.OriginalAclSddl, [StringComparison]::Ordinal)) {
      throw "Rollback ACL verification failed: $($Record.Path)"
    }
  } else {
    $lock = $null
    try {
      $lock = [System.IO.File]::Open(
        $Record.Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)
      )
      [byte[]]$currentBytes = Read-LockedStreamBytes -Stream $lock
      if (-not (Test-ByteArraysEqual -Left $currentBytes -Right ([byte[]]$Record.InstalledBytes)) -or
          -not [string]::Equals((Get-PathAclSddl -Path $Record.Path), [string]$Record.InstalledAclSddl, [StringComparison]::Ordinal)) {
        throw "Rollback refused to delete a concurrently changed file: $($Record.Path)"
      }
      [System.IO.File]::Delete($Record.Path)
    } finally {
      if ($null -ne $lock) { $lock.Dispose() }
    }
  }
}

function Invoke-RemoteInstallerTestFault {
  param([Parameter(Mandatory = $true)][string]$Phase)

  if ($env:CODEX_NTFY_TEST_MODE -eq '1' -and
      [string]$env:CODEX_NTFY_TEST_REMOTE_INSTALL_FAIL_AFTER -eq $Phase) {
    throw "Injected remote installer failure after phase: $Phase"
  }
}

function Assert-RemoteInstallerInputs {
  $required = @($ScriptPath, $PrivateConfig)
  if (-not $SkipScheduledTask) {
    $required += $HiddenWatcherPath
  }
  foreach ($path in $required) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw "Remote notifier files are incomplete: missing $path"
    }
  }

  foreach ($path in @($ScriptPath, $WatcherPath, $HiddenWatcherPath)) {
    if (Test-Path -LiteralPath $path -PathType Leaf) {
      [void](Read-StrictUtf8Text -Path $path -MaxBytes 16777216)
    }
  }
  if (Test-Path -LiteralPath $ConfigPath) {
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
      throw "Managed text destination is not a file: $ConfigPath"
    }
    [void](Read-StrictUtf8Text -Path $ConfigPath -MaxBytes 4194304)
  }

  $privateDocument = Read-StrictJsonFile -Path $PrivateConfig -MaxBytes 4194304
  if ($null -eq $privateDocument -or $privateDocument -isnot [System.Management.Automation.PSCustomObject]) {
    throw "$PrivateConfig must contain a JSON object."
  }
  if (Test-Path -LiteralPath $HooksPath) {
    if (-not (Test-Path -LiteralPath $HooksPath -PathType Leaf)) {
      throw "Managed text destination is not a file: $HooksPath"
    }
    $hooksDocument = Read-StrictJsonFile -Path $HooksPath -AllowEmpty -MaxBytes 4194304
    if ($null -eq $hooksDocument -or $hooksDocument -isnot [System.Management.Automation.PSCustomObject]) {
      throw "$HooksPath must contain a JSON object."
    }
  }
}

function Test-ManagedHookHandler {
  param([object]$Handler)

  if ($null -eq $Handler -or $Handler -isnot [System.Management.Automation.PSCustomObject]) {
    return $false
  }
  foreach ($field in @('command', 'commandWindows', 'command_windows')) {
    $property = $Handler.PSObject.Properties[$field]
    if ($null -ne $property -and $property.Value -is [string] -and
        $property.Value -match '(?i)(?:^|[\\/])notify-ntfy(?:\.ps1|\.py|-wsl\.sh)(?=$|[\s''"])') {
      return $true
    }
  }
  return $false
}

function Get-StopHookPlan {
  param(
    [string]$Path,
    [string]$Command,
    [AllowNull()][string]$OriginalText = $null
  )

  $original = if ($PSBoundParameters.ContainsKey('OriginalText')) {
    [string]$OriginalText
  } elseif (Test-Path -LiteralPath $Path) {
    Read-StrictUtf8Text -Path $Path -MaxBytes 4194304
  } else { '' }
  try {
    $document = if ([string]::IsNullOrWhiteSpace($original)) {
      [pscustomobject][ordered]@{}
    } else {
      ConvertFrom-StrictJsonText -Text $original
    }
  } catch {
    throw "Invalid JSON in ${Path}: $($_.Exception.Message)"
  }
  if ($null -eq $document -or $document -isnot [System.Management.Automation.PSCustomObject]) {
    throw "$Path must contain a JSON object."
  }
  $hooksProperty = $document.PSObject.Properties['hooks']
  if ($null -eq $hooksProperty) {
    Add-Member -InputObject $document -MemberType NoteProperty -Name 'hooks' -Value ([pscustomobject][ordered]@{})
    $hooksProperty = $document.PSObject.Properties['hooks']
  } elseif ($null -eq $hooksProperty.Value -or $hooksProperty.Value -isnot [System.Management.Automation.PSCustomObject]) {
    throw "hooks in $Path must contain a JSON object."
  }
  $hookEvents = $hooksProperty.Value

  foreach ($eventProperty in @($hookEvents.PSObject.Properties)) {
    if ($eventProperty.Value -isnot [array]) {
      if ($eventProperty.Name -eq 'Stop') { throw "hooks.Stop in $Path must contain a JSON array." }
      continue
    }
    $filteredGroups = New-Object 'System.Collections.Generic.List[object]'
    $removedFromEvent = $false
    foreach ($group in @($eventProperty.Value)) {
      if ($null -eq $group -or $group -isnot [System.Management.Automation.PSCustomObject]) {
        $filteredGroups.Add($group)
        continue
      }
      $handlersProperty = $group.PSObject.Properties['hooks']
      if ($null -eq $handlersProperty -or $handlersProperty.Value -isnot [array]) {
        $filteredGroups.Add($group)
        continue
      }
      $filteredHandlers = New-Object 'System.Collections.Generic.List[object]'
      $removedFromGroup = $false
      foreach ($handler in @($handlersProperty.Value)) {
        if (Test-ManagedHookHandler -Handler $handler) {
          $removedFromGroup = $true
          $removedFromEvent = $true
        } else {
          $filteredHandlers.Add($handler)
        }
      }
      if ($filteredHandlers.Count -gt 0) {
        $handlersProperty.Value = @($filteredHandlers.ToArray())
        $filteredGroups.Add($group)
      } elseif (-not $removedFromGroup) {
        $filteredGroups.Add($group)
      }
    }
    if ($filteredGroups.Count -gt 0 -or -not $removedFromEvent) {
      $eventProperty.Value = @($filteredGroups.ToArray())
    } else {
      $hookEvents.PSObject.Properties.Remove($eventProperty.Name)
    }
  }

  $stopProperty = $hookEvents.PSObject.Properties['Stop']
  if ($null -eq $stopProperty) {
    Add-Member -InputObject $hookEvents -MemberType NoteProperty -Name 'Stop' -Value @()
    $stopProperty = $hookEvents.PSObject.Properties['Stop']
  } elseif ($stopProperty.Value -isnot [array]) {
    throw "hooks.Stop in $Path must contain a JSON array."
  }
  $managedGroup = [pscustomobject][ordered]@{
    hooks = @(
      [pscustomobject][ordered]@{
        type = 'command'
        command = $Command
        timeout = 30
      }
    )
  }
  $stopProperty.Value = @(@($stopProperty.Value) + $managedGroup)
  $rendered = ($document | ConvertTo-Json -Depth 32) + [Environment]::NewLine
  return [pscustomobject]@{
    OriginalText = $original
    RenderedText = $rendered
  }
}

function Get-StateDirectorySnapshot {
  param([Parameter(Mandatory = $true)][string]$Path)

  if ((Test-Path -LiteralPath $Path) -and -not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "State destination is not a directory: $Path"
  }
  $exists = Test-Path -LiteralPath $Path -PathType Container
  $entries = New-Object 'System.Collections.Generic.List[object]'
  if ($exists) {
    $root = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    foreach ($item in @(Get-ChildItem -LiteralPath $Path -Force -Recurse | Sort-Object FullName)) {
      if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "State transaction refuses reparse points: $($item.FullName)"
      }
      $relative = $item.FullName.Substring($root.Length).TrimStart('\')
      $digest = ''
      if (-not $item.PSIsContainer) {
        [byte[]]$content = [System.IO.File]::ReadAllBytes($item.FullName)
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $digest = [Convert]::ToBase64String($sha.ComputeHash($content)) } finally { $sha.Dispose() }
      }
      $entries.Add([pscustomobject][ordered]@{
          Relative = $relative
          Directory = [bool]$item.PSIsContainer
          Digest = $digest
          Acl = Get-PathAclSddl -Path $item.FullName
        })
    }
  }
  $entryArray = @($entries.ToArray())
  $signature = if ($entryArray.Count -eq 0) { '[]' } else { $entryArray | ConvertTo-Json -Depth 4 -Compress }
  return [pscustomobject]@{
    Exists = [bool]$exists
    RootAcl = if ($exists) { Get-PathAclSddl -Path $Path } else { $null }
    Entries = $entryArray
    Signature = [string]$signature
  }
}

function New-StateTransactionRecord {
  param([Parameter(Mandatory = $true)][string]$Path)

  $snapshot = Get-StateDirectorySnapshot -Path $Path
  return [pscustomobject]@{
    Path = [System.IO.Path]::GetFullPath($Path)
    Original = $snapshot
    Installed = $snapshot
    Mutated = $false
  }
}

function Update-StateTransactionState {
  param([Parameter(Mandatory = $true)][object]$Record)

  $Record.Installed = Get-StateDirectorySnapshot -Path $Record.Path
  $Record.Mutated = $true
}

function Assert-StateTransactionState {
  param([Parameter(Mandatory = $true)][object]$Record)

  $current = Get-StateDirectorySnapshot -Path $Record.Path
  if ([bool]$current.Exists -ne [bool]$Record.Installed.Exists -or
      -not [string]::Equals([string]$current.RootAcl, [string]$Record.Installed.RootAcl, [StringComparison]::Ordinal) -or
      -not [string]::Equals([string]$current.Signature, [string]$Record.Installed.Signature, [StringComparison]::Ordinal)) {
    throw "State directory changed concurrently: $($Record.Path)"
  }
}

function Restore-StateTransactionRecord {
  param([Parameter(Mandatory = $true)][object]$Record)

  if (-not [bool]$Record.Mutated) { return }
  Assert-StateTransactionState -Record $Record
  $originalNames = @{}
  foreach ($entry in @($Record.Original.Entries)) { $originalNames[[string]$entry.Relative] = $true }
  $added = @($Record.Installed.Entries | Where-Object { -not $originalNames.ContainsKey([string]$_.Relative) } |
      Sort-Object @{ Expression = { ([string]$_.Relative).Length }; Descending = $true })
  foreach ($entry in $added) {
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $Record.Path ([string]$entry.Relative)))
    $prefix = $Record.Path.TrimEnd('\') + '\'
    if (-not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
      throw "State rollback path escaped the managed directory: $candidate"
    }
    if ([bool]$entry.Directory) {
      [System.IO.Directory]::Delete($candidate, $false)
    } else {
      [System.IO.File]::Delete($candidate)
    }
  }
  if ([bool]$Record.Original.Exists) {
    Set-PathAclSddl -Path $Record.Path -Sddl ([string]$Record.Original.RootAcl)
  } else {
    [System.IO.Directory]::Delete($Record.Path, $false)
  }
}

function Get-RemoteTaskPlan {
  param(
    [Parameter(Mandatory = $true)][string]$Identity,
    [Parameter(Mandatory = $true)][string]$WscriptPath
  )

  if ($SkipScheduledTask) { return $null }
  $testTaskPath = [string]$env:CODEX_NTFY_TEST_REMOTE_TASK_PATH
  if (-not [string]::IsNullOrWhiteSpace($testTaskPath)) {
    if ($env:CODEX_NTFY_TEST_MODE -ne '1') { throw 'Remote task simulation is available only in explicit test mode.' }
    $fullTaskPath = [System.IO.Path]::GetFullPath($testTaskPath)
    $homePrefix = [System.IO.Path]::GetFullPath($HomePath).TrimEnd('\') + '\'
    if (-not $fullTaskPath.StartsWith($homePrefix, [StringComparison]::OrdinalIgnoreCase)) {
      throw 'Remote task simulation path must remain inside the installer directory.'
    }
    $record = New-FileTransactionRecord -Path $fullTaskPath
    if ($record.OriginalExists) {
      $existingText = Read-StrictUtf8Text -Path $fullTaskPath -MaxBytes 1048576
      if ($existingText -notmatch '(?i)(?:watch-codex-ntfy|notify-ntfy)') {
        throw "Scheduled task '$TaskName' already exists but is unrelated; refusing to overwrite it."
      }
    }
    $rendered = "managed=watch-codex-ntfy`r`nexecute=$WscriptPath`r`narguments=//B //Nologo `"$HiddenWatcherPath`"`r`n"
    return [pscustomobject]@{
      Kind = 'simulated'
      FileRecord = $record
      Rendered = $rendered
      Mutated = $false
      State = if ($record.OriginalExists) { 'Ready' } else { 'Absent' }
    }
  }

  $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($null -ne $existingTask) {
    $owned = $false
    foreach ($existingAction in @($existingTask.Actions)) {
      $description = '{0} {1} {2}' -f $existingAction.Execute, $existingAction.Arguments, $existingAction.WorkingDirectory
      if ($description -match '(?i)(?:watch-codex-ntfy|notify-ntfy)') { $owned = $true; break }
    }
    if (-not $owned) { throw "Scheduled task '$TaskName' already exists but is unrelated; refusing to overwrite it." }
  }
  $action = New-ScheduledTaskAction -Execute $WscriptPath -Argument ('//B //Nologo "{0}"' -f $HiddenWatcherPath) -WorkingDirectory $HomePath
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
  $principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
  return [pscustomobject]@{
    Kind = 'native'
    OriginalExists = $null -ne $existingTask
    OriginalXml = if ($null -ne $existingTask) { Export-ScheduledTask -TaskName $TaskName } else { $null }
    OriginalState = if ($null -ne $existingTask) { [string]$existingTask.State } else { 'Absent' }
    Action = $action
    Trigger = $trigger
    Principal = $principal
    Settings = $settings
    ExpectedExecute = $WscriptPath
    ExpectedArguments = ('//B //Nologo "{0}"' -f $HiddenWatcherPath)
    Mutated = $false
    InstalledXml = $null
    State = if ($null -ne $existingTask) { [string]$existingTask.State } else { 'Absent' }
  }
}

function Register-RemoteTaskPlan {
  param([Parameter(Mandatory = $true)][object]$Plan)

  if ($Plan.Kind -eq 'simulated') {
    Set-ManagedFileText -Record $Plan.FileRecord -Content ([string]$Plan.Rendered)
    $Plan.Mutated = $true
    $Plan.State = 'Ready'
    return
  }
  Register-ScheduledTask -TaskName $TaskName -Action $Plan.Action -Trigger $Plan.Trigger -Principal $Plan.Principal -Settings $Plan.Settings -Description 'Durable ntfy worker for Codex completion notifications.' -Force | Out-Null
  $Plan.Mutated = $true
  $Plan.InstalledXml = Export-ScheduledTask -TaskName $TaskName
  $registeredAction = (Get-ScheduledTask -TaskName $TaskName).Actions | Select-Object -First 1
  if ([string]$registeredAction.Execute -ne [string]$Plan.ExpectedExecute -or
      [string]$registeredAction.Arguments -ne [string]$Plan.ExpectedArguments) {
    throw 'Remote scheduled worker action does not match the installed notifier.'
  }
  $Plan.State = [string](Get-ScheduledTask -TaskName $TaskName).State
}

function Start-RemoteTaskPlan {
  param([Parameter(Mandatory = $true)][object]$Plan)

  if ($Plan.Kind -eq 'simulated') {
    $Plan.State = 'Running'
    return
  }
  Start-ScheduledTask -TaskName $TaskName
  Start-Sleep -Seconds 2
  $Plan.State = [string](Get-ScheduledTask -TaskName $TaskName).State
}

function Restore-RemoteTaskPlan {
  param([AllowNull()][object]$Plan)

  if ($null -eq $Plan -or -not [bool]$Plan.Mutated) { return }
  if ($Plan.Kind -eq 'simulated') {
    Restore-FileTransactionRecord -Record $Plan.FileRecord
    return
  }
  $currentTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($null -eq $currentTask) { throw 'Scheduled task changed concurrently before rollback (now absent).' }
  $currentXml = Export-ScheduledTask -TaskName $TaskName
  if ([string]::IsNullOrWhiteSpace([string]$Plan.InstalledXml) -or
      -not [string]::Equals([string]$currentXml, [string]$Plan.InstalledXml, [StringComparison]::Ordinal)) {
    throw 'Scheduled task changed concurrently before rollback; refusing to overwrite it.'
  }
  if ([string]$currentTask.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
  }
  if ([bool]$Plan.OriginalExists) {
    Register-ScheduledTask -TaskName $TaskName -Xml ([string]$Plan.OriginalXml) -Force | Out-Null
    if ([string]$Plan.OriginalState -eq 'Running') { Start-ScheduledTask -TaskName $TaskName }
  } else {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }
}

Assert-RemoteInstallerInputs

# Complete every semantic check and build every rendered payload before the
# transaction creates a backup or changes config, hooks, ACLs, state, or task.
$privateRecord = New-FileTransactionRecord -Path $PrivateConfig -MaxBytes 4194304
$configRecord = New-FileTransactionRecord -Path $ConfigPath -MaxBytes 4194304
$hooksRecord = New-FileTransactionRecord -Path $HooksPath -MaxBytes 4194304
$privateOriginal = ConvertFrom-StrictUtf8Bytes -Bytes ([byte[]]$privateRecord.OriginalBytes) -Context "Managed text file $PrivateConfig"
try {
  $privateObject = ConvertFrom-StrictJsonText -Text $privateOriginal
} catch {
  throw "Invalid JSON in $($PrivateConfig): $($_.Exception.Message)"
}
$privateChanged = $false
foreach ($default in @(
    @('include_message', $false),
    @('include_thread_title', $false),
    @('include_task_link', $false),
    @('include_task_link_action', $false),
    @('allow_insecure_auth', $false),
    @('priority', 3),
    @('tags', @('white_check_mark')),
    @('max_message_chars', 180),
    @('markdown', $false),
    @('idle_detection_mode', 'strict'),
    @('idle_grace_seconds', 1.5),
    @('idle_probe_grace_seconds', 30),
    @('unknown_retry_max_seconds', 60),
    @('goal_aware', $true),
    @('goal_poll_seconds', 1),
    @('subagent_orphan_seconds', 1800),
    @('suppress_technical_turns', $true),
    @('watch_rollouts', $true),
    @('watch_scan_seconds', 2),
    @('watch_discovery_seconds', 60),
    @('watch_cursor_batch_size', 64),
    @('watch_remote_timeout_seconds', 90),
    @('watch_initial_replay_seconds', 15),
    @('dead_retention_days', 30)
  )) {
  if ($null -eq $privateObject.PSObject.Properties[$default[0]]) {
    Add-Member -InputObject $privateObject -MemberType NoteProperty -Name $default[0] -Value $default[1]
    $privateChanged = $true
  }
}
$configuredTags = @($privateObject.tags)
if ($configuredTags.Count -eq 2 -and
    [string]$configuredTags[0] -eq 'computer' -and
    [string]$configuredTags[1] -eq 'white_check_mark') {
  $privateObject.tags = @('white_check_mark')
  $privateChanged = $true
}
$watchRootsProperty = $privateObject.PSObject.Properties['watch_roots']
if ($null -eq $watchRootsProperty) {
  Add-Member -InputObject $privateObject -MemberType NoteProperty -Name 'watch_roots' -Value @()
  $privateChanged = $true
} elseif (@($watchRootsProperty.Value).Count -ne 0) {
  $watchRootsProperty.Value = @()
  $privateChanged = $true
}
$workerSqlitePath = if ([string]::IsNullOrWhiteSpace($env:CODEX_SQLITE_HOME)) {
  $HomePath
} else {
  [System.IO.Path]::GetFullPath($env:CODEX_SQLITE_HOME)
}
$workerSqliteProperty = $privateObject.PSObject.Properties['worker_sqlite_path']
if ($null -eq $workerSqliteProperty) {
  Add-Member -InputObject $privateObject -MemberType NoteProperty -Name 'worker_sqlite_path' -Value $workerSqlitePath
  $privateChanged = $true
} elseif ([string]$workerSqliteProperty.Value -ne $workerSqlitePath) {
  $workerSqliteProperty.Value = $workerSqlitePath
  $privateChanged = $true
}
$privateRendered = if ($privateChanged) {
  ($privateObject | ConvertTo-Json -Depth 8) + [Environment]::NewLine
} else {
  $privateOriginal
}

$alias = $Origin -replace '^SSH:', ''
$effectiveOrigin = 'SSH:' + $env:COMPUTERNAME
if (-not [string]::IsNullOrWhiteSpace($alias) -and $alias -notin @('Windows', $env:COMPUTERNAME)) {
  $effectiveOrigin += ' (' + $alias + ')'
}
$windowsPowerShellPath = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
$escaped = $ScriptPath.Replace('\', '\\').Replace('"', '\"')
$escapedOrigin = $effectiveOrigin.Replace('\', '\\').Replace('"', '\"')
$escapedPowerShell = $windowsPowerShellPath.Replace('\', '\\').Replace('"', '\"')
$notifyLine = 'notify = ["' + $escapedPowerShell + '", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", "' + $escaped + '", "-Origin", "' + $escapedOrigin + '"]'
$configOriginal = if ($configRecord.OriginalExists) {
  ConvertFrom-StrictUtf8Bytes -Bytes ([byte[]]$configRecord.OriginalBytes) -Context "Managed text file $ConfigPath"
} else {
  ''
}
$configRendered = $configOriginal
$table = [regex]::Match($configRendered, '(?m)^[ \t]*\[')
$rootText = if ($table.Success) { $configRendered.Substring(0, $table.Index) } else { $configRendered }
$notifyMatch = [regex]::Match($rootText, '(?m)^[ \t]*notify[ \t]*=[^\r\n]*(?=\r?$)')
if ($notifyMatch.Success) {
  if ($notifyMatch.Value -notmatch 'notify-ntfy\.ps1') {
    throw 'Existing remote notify command is unrelated; refusing to overwrite it.'
  }
  if ($notifyMatch.Value -ne $notifyLine) {
    $configRendered = $configRendered.Remove($notifyMatch.Index, $notifyMatch.Length).Insert($notifyMatch.Index, $notifyLine)
  }
} elseif ($table.Success) {
  $configRendered = $configRendered.Insert($table.Index, $notifyLine + [Environment]::NewLine + [Environment]::NewLine)
} else {
  $configRendered = $configRendered.TrimEnd() + [Environment]::NewLine + [Environment]::NewLine + $notifyLine + [Environment]::NewLine
}

$hookOrigin = $effectiveOrigin.Replace('"', '\"')
$hookCommand = '"{0}" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}" -Origin "{2}" -HookEvent' -f $windowsPowerShellPath, $ScriptPath, $hookOrigin
$hookOriginal = if ($hooksRecord.OriginalExists) {
  ConvertFrom-StrictUtf8Bytes -Bytes ([byte[]]$hooksRecord.OriginalBytes) -Context "Managed text file $HooksPath"
} else {
  ''
}
$hookPlan = Get-StopHookPlan -Path $HooksPath -Command $hookCommand -OriginalText $hookOriginal

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$wscript = Join-Path $env:WINDIR 'System32\wscript.exe'
$taskPlan = Get-RemoteTaskPlan -Identity $identity -WscriptPath $wscript

$stateRecord = New-StateTransactionRecord -Path $StatePath
$backupRecord = $null
$backupRoot = Join-Path $HomePath 'ntfy-backups'
if ($hooksRecord.OriginalExists -and (Test-Path -LiteralPath $backupRoot -PathType Container)) {
  $latestBackup = Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -First 1
  if ($null -ne $latestBackup) {
    $savedHooks = Join-Path $latestBackup.FullName 'hooks.json'
    if (-not (Test-Path -LiteralPath $savedHooks)) {
      $backupRecord = New-FileTransactionRecord -Path $savedHooks
    }
  }
}
$fileRecords = New-Object 'System.Collections.Generic.List[object]'
if ($null -ne $backupRecord) { $fileRecords.Add($backupRecord) }
$fileRecords.Add($privateRecord)
$fileRecords.Add($configRecord)
$fileRecords.Add($hooksRecord)

try {
  if ($null -ne $backupRecord) {
    Set-ManagedFileBytes -Record $backupRecord -Content ([byte[]]$hooksRecord.OriginalBytes)
  }
  Invoke-RemoteInstallerTestFault -Phase 'backup'

  Set-ManagedFileText -Record $privateRecord -Content $privateRendered
  Invoke-RemoteInstallerTestFault -Phase 'private-config'

  Set-ManagedFileText -Record $configRecord -Content $configRendered
  Invoke-RemoteInstallerTestFault -Phase 'codex-config'

  Set-ManagedFileText -Record $hooksRecord -Content ([string]$hookPlan.RenderedText)
  Invoke-RemoteInstallerTestFault -Phase 'hooks'

  if (-not (Test-Path -LiteralPath $StatePath -PathType Container)) {
    New-Item -ItemType Directory -Path $StatePath | Out-Null
  }
  Protect-PrivatePath -Path $StatePath
  Update-StateTransactionState -Record $stateRecord
  Invoke-RemoteInstallerTestFault -Phase 'state'

  $doctorLines = @(& $windowsPowerShellPath -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $ScriptPath -Doctor)
  $doctorExitCode = $LASTEXITCODE
  Update-StateTransactionState -Record $stateRecord
  if ($doctorExitCode -ne 0) { throw "Remote notifier doctor failed with exit code $doctorExitCode." }
  $doctorText = $doctorLines -join [Environment]::NewLine
  Assert-SafeUnicodeScalarText -Value $doctorText -Context 'Remote notifier doctor output'
  if ($Utf8StrictNoBom.GetByteCount($doctorText) -gt 1048576) {
    throw 'Remote notifier doctor output exceeds the supported byte limit.'
  }
  try {
    $doctor = ConvertFrom-StrictJsonText -Text $doctorText
  } catch {
    throw "Remote notifier doctor returned invalid JSON: $($_.Exception.Message)"
  }
  Invoke-RemoteInstallerTestFault -Phase 'doctor'

  $workerState = 'skipped'
  if ($null -ne $taskPlan) {
    Register-RemoteTaskPlan -Plan $taskPlan
    Invoke-RemoteInstallerTestFault -Phase 'task-register'
    Start-RemoteTaskPlan -Plan $taskPlan
    Invoke-RemoteInstallerTestFault -Phase 'task-start'
    $workerState = [string]$taskPlan.State
    if ($workerState -ne 'Running') {
      throw "Remote worker did not start (state: $workerState)."
    }
  }

  Write-Warning 'Codex will skip the new Stop hook until you review and trust it with /hooks on this host.'
  Write-Output ('OK computer=' + $env:COMPUTERNAME + ' user=' + $env:USERNAME + ' topic_configured=' + [bool]$doctor.topic_configured + ' worker=' + $workerState)
} catch {
  $installError = $_
  $rollbackErrors = New-Object 'System.Collections.Generic.List[string]'
  try {
    Restore-RemoteTaskPlan -Plan $taskPlan
  } catch {
    $rollbackErrors.Add($_.Exception.Message)
  }
  try {
    Restore-StateTransactionRecord -Record $stateRecord
  } catch {
    $rollbackErrors.Add($_.Exception.Message)
  }
  for ($index = $fileRecords.Count - 1; $index -ge 0; $index--) {
    try {
      Restore-FileTransactionRecord -Record $fileRecords[$index]
    } catch {
      $rollbackErrors.Add($_.Exception.Message)
    }
  }
  if ($rollbackErrors.Count -gt 0) {
    throw "$($installError.Exception.Message) Rollback failed closed: $($rollbackErrors -join ' | ')"
  }
  throw $installError
}
