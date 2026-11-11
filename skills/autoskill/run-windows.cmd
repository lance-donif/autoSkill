@echo off
setlocal
set "AUTOSKILL_VENDOR=%~dp0vendor\windows-x64"
if not exist "%AUTOSKILL_VENDOR%\python\python.exe" (
  echo Portable dependencies are missing. Use the complete Windows x64 package. No download will be attempted.
  exit /b 2
)
"%AUTOSKILL_VENDOR%\python\python.exe" "%~dp0scripts\windows_entry.py" %*
exit /b %errorlevel%
