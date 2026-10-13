@echo off
chcp 65001 >nul
rem Запуск без ярлыка: двойной щелчок по этому файлу.
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo Сначала запустите install.ps1 - он поставит всё нужное.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0app\app.py"
