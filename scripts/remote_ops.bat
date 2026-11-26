@echo off
REM Remote git-ops: run the owner's signed plan fetched from GitHub.
REM Only normalized git operations (src/syncnote/remote_ops.py); inert
REM until a signing secret is set (syncnote secrets set remote_ops).
call "%~dp0_common.bat"
"%PY%" -m syncnote remote-ops poll %*
