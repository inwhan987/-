@echo off
REM ============================================================
REM  folder_scheduler.exe 빌드 스크립트 (Windows 에서 실행)
REM  - Python 3 와 pip 가 설치되어 있어야 합니다.
REM ============================================================
setlocal

echo [1/3] PyInstaller 설치 확인...
python -m pip install --upgrade pyinstaller || goto :error

echo [2/3] exe 빌드...
python -m PyInstaller --onefile --console --name folder_scheduler folder_scheduler.py || goto :error

echo [3/3] 완료. dist\folder_scheduler.exe 생성됨.
echo.
echo 다음 순서로 사용하세요:
echo   1) dist\folder_scheduler.exe 를 원하는 폴더로 옮깁니다.
echo   2) 같은 폴더에 folder_scheduler.ini 를 만들고 target_folder 를 지정합니다.
echo      (exe 를 한 번 실행하면 ini 파일이 자동 생성됩니다)
echo   3) 관리자 권한으로  folder_scheduler.exe install  실행 -> 스케줄러 등록
echo.
goto :eof

:error
echo.
echo 빌드 중 오류가 발생했습니다.
exit /b 1
