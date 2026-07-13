"""
폴더 자동 압축/해제 스케줄러

- 저녁(기본 18:00): 지정한 폴더를 zip으로 백업 (원본 폴더는 유지)
- 아침(기본 09:10): zip을 풀어서 폴더에 덮어쓰기

exe 하나로 동작하며, 아래 명령을 지원한다.

    folder_scheduler.exe install     # 작업 스케줄러에 압축/해제 작업 2개 등록
    folder_scheduler.exe uninstall   # 등록한 작업 삭제
    folder_scheduler.exe status      # 현재 설정/작업 상태 확인
    folder_scheduler.exe zip         # 지금 바로 압축 (스케줄러가 저녁에 호출)
    folder_scheduler.exe unzip       # 지금 바로 해제 (스케줄러가 아침에 호출)

설정은 exe 옆의 folder_scheduler.ini 파일에서 읽는다.
"""

import configparser
import datetime as dt
import os
import subprocess
import sys
import zipfile

APP_NAME = "FolderZipScheduler"
CONFIG_NAME = "folder_scheduler.ini"
LOG_NAME = "folder_scheduler.log"


def base_dir():
    """exe(또는 스크립트)가 위치한 폴더. 설정/로그는 여기에 둔다."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def program_path():
    """스케줄러에 등록할 실행 경로. exe면 exe 자신, 아니면 python 스크립트."""
    if getattr(sys, "frozen", False):
        return f'"{os.path.abspath(sys.executable)}"'
    return f'"{os.path.abspath(sys.executable)}" "{os.path.abspath(__file__)}"'


def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(os.path.join(base_dir(), LOG_NAME), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def default_config_text():
    return (
        "[settings]\n"
        "; 압축할 대상 폴더 (필수). 예: C:\\Users\\me\\Documents\\work\n"
        "target_folder = C:\\변경하세요\\대상폴더\n"
        "\n"
        "; 만들 zip 파일 경로. 비우면 대상폴더 옆에 <폴더이름>.zip 으로 생성\n"
        "zip_path =\n"
        "\n"
        "; 압축 시각 (24시간, HH:MM)\n"
        "zip_time = 18:00\n"
        "\n"
        "; 해제 시각 (24시간, HH:MM)\n"
        "unzip_time = 09:10\n"
    )


def config_path():
    return os.path.join(base_dir(), CONFIG_NAME)


def load_config():
    path = config_path()
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(default_config_text())
        log(f"설정 파일을 새로 만들었습니다: {path}")
        log("target_folder 값을 실제 폴더 경로로 수정한 뒤 다시 실행하세요.")
        return None

    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if not parser.has_section("settings"):
        log(f"설정 파일에 [settings] 섹션이 없습니다: {path}")
        return None

    s = parser["settings"]
    target = s.get("target_folder", "").strip()
    zip_path = s.get("zip_path", "").strip()
    zip_time = s.get("zip_time", "18:00").strip()
    unzip_time = s.get("unzip_time", "09:10").strip()

    if not target or "변경하세요" in target:
        log("설정의 target_folder 값을 실제 폴더 경로로 지정하세요.")
        return None

    if not zip_path:
        zip_path = os.path.join(
            os.path.dirname(os.path.normpath(target)),
            os.path.basename(os.path.normpath(target)) + ".zip",
        )

    return {
        "target": os.path.normpath(target),
        "zip_path": os.path.normpath(zip_path),
        "zip_time": zip_time,
        "unzip_time": unzip_time,
    }


def do_zip(cfg):
    target = cfg["target"]
    zip_path = cfg["zip_path"]

    if not os.path.isdir(target):
        log(f"대상 폴더가 없습니다: {target}")
        return 1

    parent = os.path.dirname(target)
    folder_name = os.path.basename(target)
    tmp_path = zip_path + ".tmp"

    os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)

    count = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 폴더 이름을 zip 루트로 포함해서 저장 -> 해제 시 폴더가 그대로 복원됨
            for root, dirs, files in os.walk(target):
                # 빈 폴더도 보존
                if not files and not dirs:
                    arc = os.path.relpath(root, parent) + "/"
                    zf.writestr(arc, "")
                for name in files:
                    full = os.path.join(root, name)
                    arc = os.path.relpath(full, parent)
                    zf.write(full, arc)
                    count += 1
        # 원자적 교체 (기존 zip이 있으면 덮어쓰기)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        os.replace(tmp_path, zip_path)
    except OSError as e:
        log(f"압축 실패: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return 1

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    log(f"압축 완료: '{folder_name}' -> {zip_path} (파일 {count}개, {size_mb:.1f} MB) / 원본 유지")
    return 0


def do_unzip(cfg):
    target = cfg["target"]
    zip_path = cfg["zip_path"]

    if not os.path.isfile(zip_path):
        log(f"압축 파일이 없습니다: {zip_path}")
        return 1

    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            # zip 내부 경로가 대상 폴더 밖으로 벗어나지 않는지 확인 (Zip Slip 방지)
            base = os.path.abspath(parent)
            for member in zf.namelist():
                dest = os.path.abspath(os.path.join(parent, member))
                if not dest.startswith(base + os.sep) and dest != base:
                    log(f"안전하지 않은 경로가 있어 중단합니다: {member}")
                    return 1
            zf.extractall(parent)
    except (OSError, zipfile.BadZipFile) as e:
        log(f"해제 실패: {e}")
        return 1

    log(f"해제 완료: {zip_path} -> {parent} (폴더 '{os.path.basename(target)}' 덮어쓰기)")
    return 0


def schtasks(args):
    return subprocess.run(
        ["schtasks"] + args, capture_output=True, text=True
    )


def do_install(cfg):
    if os.name != "nt":
        log("install 은 Windows 에서만 동작합니다.")
        return 1

    prog = program_path()
    tasks = [
        (f"{APP_NAME}_Zip", "zip", cfg["zip_time"]),
        (f"{APP_NAME}_Unzip", "unzip", cfg["unzip_time"]),
    ]

    ok = True
    for name, action, time_str in tasks:
        # /TR 은 exe 경로 + 인자. 큰따옴표를 포함하므로 문자열로 전달
        run_cmd = f'{prog} {action}'
        r = schtasks([
            "/Create", "/TN", name, "/TR", run_cmd,
            "/SC", "DAILY", "/ST", time_str, "/F",
        ])
        if r.returncode == 0:
            log(f"작업 등록: {name} (매일 {time_str}, {action})")
        else:
            ok = False
            log(f"작업 등록 실패: {name} -> {r.stderr.strip() or r.stdout.strip()}")

    if ok:
        log("설치 완료. 이제 매일 지정 시각에 자동으로 압축/해제됩니다.")
    else:
        log("일부 작업 등록에 실패했습니다. 관리자 권한 명령 프롬프트에서 다시 시도해 보세요.")
    return 0 if ok else 1


def do_uninstall():
    if os.name != "nt":
        log("uninstall 은 Windows 에서만 동작합니다.")
        return 1

    ok = True
    for name in (f"{APP_NAME}_Zip", f"{APP_NAME}_Unzip"):
        r = schtasks(["/Delete", "/TN", name, "/F"])
        if r.returncode == 0:
            log(f"작업 삭제: {name}")
        else:
            ok = False
            log(f"작업 삭제 실패(또는 없음): {name} -> {r.stderr.strip() or r.stdout.strip()}")
    return 0 if ok else 1


def do_status(cfg):
    log(f"설정 파일: {config_path()}")
    log(f"대상 폴더: {cfg['target']}")
    log(f"zip 경로 : {cfg['zip_path']}")
    log(f"압축 시각: 매일 {cfg['zip_time']}")
    log(f"해제 시각: 매일 {cfg['unzip_time']}")
    if os.name == "nt":
        for name in (f"{APP_NAME}_Zip", f"{APP_NAME}_Unzip"):
            r = schtasks(["/Query", "/TN", name])
            state = "등록됨" if r.returncode == 0 else "미등록"
            log(f"스케줄러 작업 {name}: {state}")
    return 0


USAGE = (
    "사용법: folder_scheduler[.exe] <명령>\n"
    "  install    작업 스케줄러에 압축/해제 등록\n"
    "  uninstall  등록한 작업 삭제\n"
    "  status     설정/작업 상태 확인\n"
    "  zip        지금 바로 압축\n"
    "  unzip      지금 바로 해제\n"
)


def main(argv):
    cmd = argv[1].lower() if len(argv) > 1 else "status"

    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    cfg = load_config()
    if cfg is None:
        return 1

    if cmd == "zip":
        return do_zip(cfg)
    if cmd == "unzip":
        return do_unzip(cfg)
    if cmd == "install":
        return do_install(cfg)
    if cmd == "uninstall":
        return do_uninstall()
    if cmd == "status":
        return do_status(cfg)

    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
