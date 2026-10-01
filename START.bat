@echo off
cd /d "%~dp0"
title Media Bank Ultimate
if not exist ".env" (
  if exist "..\MediaBank_FRESH\.env" (
    copy /Y "..\MediaBank_FRESH\.env" ".env" >nul
    echo Copied .env from previous clean build.
  ) else if exist "..\media_bank_bot\.env" (
    copy /Y "..\media_bank_bot\.env" ".env" >nul
    echo Copied .env from old project.
  ) else (
    echo .env not found.
    echo Put your working .env into this folder.
    pause
    exit /b 1
  )
)
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv
if not exist ".venv\Scripts\python.exe" python -m venv .venv
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
 echo Dependency installation failed.
 pause
 exit /b 2
)
".venv\Scripts\python.exe" bot.py
pause
