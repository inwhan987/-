@echo off
REM 등록한 압축/해제 작업을 삭제합니다.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0folder_scheduler.ps1" uninstall
echo.
pause
