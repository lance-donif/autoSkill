@echo off
rem autoSkill entry point. Everything it needs is inside this folder; nothing is downloaded.
setlocal
set "AUTOSKILL_HOME=%~dp0"
set "AUTOSKILL_PY=%AUTOSKILL_HOME%vendor\windows-x64\python\python.exe"
rem The bundled interpreter must never pick up packages installed on this machine.
set "PYTHONNOUSERSITE=1"
if not exist "%AUTOSKILL_PY%" (
  if defined AUTOSKILL_DEV_PYTHON (
    set "AUTOSKILL_PY=%AUTOSKILL_DEV_PYTHON%"
    set "PYTHONNOUSERSITE="
  ) else (
    echo {"error": "bundled runtime not found under vendor\\windows-x64. Use the complete Windows offline package; nothing is downloaded."}
    exit /b 3
  )
)
set "PYTHONPATH=%AUTOSKILL_HOME%app"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%AUTOSKILL_PY%" -m autoskill %*
exit /b %errorlevel%
