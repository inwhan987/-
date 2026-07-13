# 폴더 자동 압축/해제 스케줄러 (PowerShell 버전)

지정한 폴더를 **매일 저녁(기본 18:00)** 에 zip으로 백업하고, **다음날 아침(기본 09:10)**
에 다시 풀어주는 Windows 유틸리티입니다.

- **파이썬 설치 불필요.** Windows에 기본 내장된 PowerShell만 사용합니다.
- 별도 설치/빌드 과정이 없습니다. 파일을 폴더에 두고 `install.bat` 만 실행하면 됩니다.
- 압축해도 **원본 폴더는 그대로 유지**됩니다 (백업 성격).
- 프로그램을 상시 켜둘 필요 없이, Windows 작업 스케줄러가 지정 시각에만 실행합니다.

## 구성 파일

| 파일 | 역할 |
|------|------|
| `folder_scheduler.ps1` | 실제 압축/해제/등록을 수행하는 본체 |
| `install.bat` | 더블클릭 → 작업 스케줄러에 압축/해제 작업 등록 |
| `uninstall.bat` | 더블클릭 → 등록한 작업 삭제 |
| `status.bat` | 더블클릭 → 현재 설정과 등록 상태 확인 |
| `folder_scheduler.ini.example` | 설정 파일 예시 |

## 설정

같은 폴더에 `folder_scheduler.ini` 파일을 둡니다. (없으면 처음 실행할 때 자동 생성됩니다)

```ini
[settings]
target_folder = C:\Users\me\Documents\work   ; 압축할 폴더
zip_path =                                    ; 비우면 폴더 옆에 <이름>.zip 생성
zip_time = 18:00                              ; 압축 시각
unzip_time = 09:10                            ; 해제 시각
```

`target_folder` 만 실제 경로로 바꾸면 됩니다.

## 사용 순서

1. 이 폴더의 파일들(`folder_scheduler.ps1`, `install.bat` 등)을 원하는 위치에 둡니다.
2. `status.bat` 을 한 번 더블클릭 → `folder_scheduler.ini` 가 생기면
   `target_folder` 를 실제 폴더 경로로 수정합니다.
3. `install.bat` 을 더블클릭합니다.
   - 권한 오류가 나면 `install.bat` 을 **우클릭 → 관리자 권한으로 실행**.
4. `status.bat` 으로 두 작업이 "등록됨" 인지 확인합니다.

이후 매일 저녁 18:00 압축, 아침 09:10 해제가 자동으로 실행됩니다.
설정(시각/폴더)을 바꾸면 `install.bat` 을 다시 실행해 반영하세요.

## 직접(수동) 실행

테스트하거나 지금 바로 돌리고 싶을 때:

```powershell
powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 zip     # 지금 압축
powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 unzip   # 지금 해제
powershell -ExecutionPolicy Bypass -File folder_scheduler.ps1 status  # 상태
```

## 동작 로그

같은 폴더의 `folder_scheduler.log` 에 실행 기록이 남습니다. 스케줄러가 언제 무엇을
했는지 여기서 확인할 수 있습니다.

## 알아둘 점

- 작업은 **로그인한 사용자 기준**으로 등록됩니다. PC가 켜져 있고 로그인된 상태에서
  지정 시각이 되면 실행됩니다. (PC가 꺼져 있으면 그 시각엔 실행되지 않습니다)
- 압축은 임시 파일(`.tmp`)에 먼저 쓰고 완료 시 교체하므로, 도중에 실패해도 기존
  zip이 깨지지 않습니다.
- 아침 해제는 zip 내용을 폴더에 덮어씁니다. 순수 백업/복원 용도에 맞춰져 있습니다.
