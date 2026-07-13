Option Explicit

Dim shell
Dim fileSystem
Dim home
Dim command
Dim powershell
Dim notifier

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
home = fileSystem.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
notifier = home & "\notify-ntfy.ps1"
shell.CurrentDirectory = home

' Supervise the notifier directly. Going through watch-codex-ntfy.ps1 used to
' require two cold PowerShell starts; on machines where AMSI/Defender scans the
' large notifier script slowly, that alone could postpone readiness for minutes.
Do
  command = """" & powershell & """ -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & notifier & """ -Worker -Continuous -PollSeconds 2"
  shell.Run command, 0, True
  WScript.Sleep 2000
Loop
