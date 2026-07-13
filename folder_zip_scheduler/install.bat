@echo off
REM 작업 스케줄러에 압축/해제 작업을 등록합니다.
REM (권한 문제로 실패하면 이 파일을 우클릭 -> "관리자 권한으로 실행")
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0folder_scheduler.ps1" install
echo.
pause
