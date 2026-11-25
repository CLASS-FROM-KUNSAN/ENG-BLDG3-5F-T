'=====================================================================
'  Run a .bat without any window (used by Task Scheduler, see register_tasks.ps1).
'  ASCII only: wscript reads a BOM-less file in the ANSI code page (cp949 on
'  Korean Windows), so Korean text here would be garbled (docs/SCRIPTS.md).
'
'  Why: a .bat started directly by Task Scheduler flashes a black console
'  window even when "hidden"; going through .vbs creates no window at all.
'
'  Usage:
'    wscript.exe hidden.vbs "C:\...\scripts\eclass_sync.bat"
'    wscript.exe hidden.vbs "C:\...\scripts\record_schedule.bat" "--once"
'
'  Note: with no window, errors are not visible. Check the log file
'  (logs\syncnote.log in the data folder; doctor.bat shows where).
'=====================================================================
Option Explicit

Dim shell, args, cmd, i
Set shell = CreateObject("WScript.Shell")
Set args = WScript.Arguments

If args.Count = 0 Then
    WScript.Echo "Usage: wscript.exe hidden.vbs ""C:\path\scripts\eclass_sync.bat"" [args...]"
    WScript.Quit 1
End If

cmd = """" & args(0) & """"
For i = 1 To args.Count - 1
    cmd = cmd & " """ & args(i) & """"
Next

' Second argument 0 = hidden window, third True = wait until it ends.
'
' Without waiting, Task Scheduler reports success at once, which makes
'     -RestartCount 2 -RestartInterval 5 minutes
'     -ExecutionTimeLimit 12 hours
' in register_tasks.ps1 useless: there is no failure to restart, and the time
' limit applies to a wscript that already ended. If Python died during class,
' the scheduler would not know and that day's classes would be lost until the
' next log on. Returning the .bat exit code lets the scheduler see failures.
Dim rc
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
