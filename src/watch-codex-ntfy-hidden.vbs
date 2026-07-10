Option Explicit

Dim shell
Dim fileSystem
Dim home
Dim command
Dim powershell

Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
home = fileSystem.GetParentFolderName(WScript.ScriptFullName)
powershell = shell.ExpandEnvironmentStrings("%WINDIR%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
shell.CurrentDirectory = home

command = """" & powershell & """ -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & home & "\watch-codex-ntfy.ps1"""
shell.Run command, 0, True
