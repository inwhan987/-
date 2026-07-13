@echo off
REM 현재 설정과 스케줄러 등록 상태를 보여줍니다.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0folder_scheduler.ps1" status
echo.
pause
