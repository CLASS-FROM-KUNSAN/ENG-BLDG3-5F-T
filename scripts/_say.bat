@REM =====================================================================
@REM  Print a user message from scripts\msg\<name>.txt (UTF-8, Korean).
@REM  Rule (docs/SCRIPTS.md "ASCII-only .bat"): every .bat is ASCII only.
@REM  cmd mis-reads multibyte lines in batch files: after a "call" returns it
@REM  can resume at a wrong byte offset and run fragments of Korean comments
@REM  as commands (reproduced: _common.bat calling _find_python.bat, in both
@REM  cp949 and UTF-8 consoles). "type" copies the file to the console without
@REM  parsing it as batch lines, so Korean text lives in msg\*.txt instead.
@REM  Call after _common.bat (it switches the console to UTF-8, chcp 65001).
@REM  Usage: call "%~dp0_say.bat" <name>
@REM  A message file without a trailing newline lets the next echo continue
@REM  on the same line (used to print a path or variable after the text).
@REM =====================================================================
@if exist "%~dp0msg\%~1.txt" (type "%~dp0msg\%~1.txt") else (echo [message missing: %~1])
@exit /b 0
