# 폴더 자동 압축/해제 스케줄러 (folder_scheduler.exe)

지정한 폴더를 **매일 저녁(기본 18:00)** 에 zip으로 백업하고, **다음날 아침(기본 09:10)**
에 다시 풀어주는 작은 Windows 유틸리티입니다.

- 압축해도 **원본 폴더는 그대로 유지**됩니다 (백업 성격).
- 아침에는 zip을 풀어 폴더에 덮어씁니다.
- exe 하나로 동작하며, `install` 한 번이면 Windows 작업 스케줄러에 자동 등록됩니다.
  프로그램을 상시 켜둘 필요가 없고, 지정한 시각에만 스케줄러가 실행합니다.

## 왜 상주형이 아니라 스케줄러 방식인가

상주형(프로그램을 계속 띄워두는 방식)은 PC를 재부팅하거나 창을 닫으면 그날은 동작하지
않습니다. 작업 스케줄러에 등록하면 로그인/부팅과 무관하게 매일 정해진 시각에 확실히
실행되므로 훨씬 안정적입니다.

## 빌드 (exe 만들기)

Windows에서 Python 3가 설치된 상태로:

```bat
build.bat
```

`dist\folder_scheduler.exe` 가 생성됩니다.

> 참고: exe 없이 Python으로 바로 실행해도 동일하게 동작합니다.
> `python folder_scheduler.py <명령>`

## 설정

exe 옆에 `folder_scheduler.ini` 파일을 둡니다. (없으면 exe를 한 번 실행할 때 자동 생성)

```ini
[settings]
target_folder = C:\Users\me\Documents\work   ; 압축할 폴더
zip_path =                                    ; 비우면 폴더 옆에 <이름>.zip 생성
zip_time = 18:00                              ; 압축 시각
unzip_time = 09:10                            ; 해제 시각
```

`target_folder` 만 실제 경로로 바꾸면 됩니다.

## 사용법

```bat
folder_scheduler.exe install     :: 작업 스케줄러에 압축/해제 작업 2개 등록
folder_scheduler.exe uninstall   :: 등록한 작업 삭제
folder_scheduler.exe status      :: 현재 설정/등록 상태 확인
folder_scheduler.exe zip         :: 지금 바로 압축 (테스트용)
folder_scheduler.exe unzip       :: 지금 바로 해제 (테스트용)
```

### 처음 설정하는 순서

1. `folder_scheduler.exe` 를 원하는 폴더에 둡니다.
2. `folder_scheduler.exe status` 를 한 번 실행 → `folder_scheduler.ini` 가 생기면
   `target_folder` 를 실제 폴더 경로로 수정합니다.
3. **관리자 권한** 명령 프롬프트에서 `folder_scheduler.exe install` 실행.
4. `folder_scheduler.exe status` 로 두 작업이 "등록됨" 인지 확인.

이후 매일 저녁 18:00 압축, 아침 09:10 해제가 자동으로 실행됩니다.

## 동작 로그

exe 옆의 `folder_scheduler.log` 에 실행 기록이 남습니다. 스케줄러가 언제 무엇을
했는지 여기서 확인할 수 있습니다.

## 안전장치

- 압축은 임시 파일(`.tmp`)에 먼저 쓰고 완료되면 교체하므로, 도중에 실패해도 기존
  zip이 깨지지 않습니다.
- 해제 시 zip 내부 경로가 대상 폴더 밖으로 벗어나면(Zip Slip) 중단합니다.
