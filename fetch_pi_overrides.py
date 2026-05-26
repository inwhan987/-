"""파이에서 현재 .env.overrides 값을 받아와 로컬에 적용.

사용법:
    python fetch_pi_overrides.py [파이IP]

예시:
    python fetch_pi_overrides.py 192.168.0.100

실행하면:
    1. 파이 웹 API에서 현재 .env.overrides 를 받아옴
    2. 로컬 .env.overrides 에 덮어씀
    3. git diff 로 변경 내용 보여줌
    4. 커밋 여부 선택
"""
import sys
import subprocess
import urllib.request
from pathlib import Path

PI_DEFAULT_IP = "192.168.0.104"
PI_PORT = 8001

def main():
    ip = sys.argv[1] if len(sys.argv) > 1 else PI_DEFAULT_IP
    url = f"http://{ip}:{PI_PORT}/api/overrides/raw"

    print(f"[fetch] {url} 에서 .env.overrides 가져오는 중...")
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        print(f"[오류] 파이 연결 실패: {e}")
        print(f"  → 파이가 켜져 있고 같은 네트워크인지 확인하세요.")
        print(f"  → IP 주소: python fetch_pi_overrides.py [파이IP]")
        sys.exit(1)

    if not content.strip():
        print("[오류] 파이에 .env.overrides 파일이 없습니다.")
        sys.exit(1)

    root = Path(__file__).parent
    ovr_path = root / ".env.overrides"

    # 현재 로컬 내용과 동일하면 스킵
    if ovr_path.exists() and ovr_path.read_text(encoding="utf-8") == content:
        print("[완료] 로컬과 파이의 .env.overrides 가 동일합니다. 변경 없음.")
        return

    ovr_path.write_text(content, encoding="utf-8")
    print(f"[완료] .env.overrides 업데이트 완료")

    # git diff 로 변경 내용 보여주기
    print("\n─── 변경 내용 ───────────────────────────────")
    diff = subprocess.run(
        ["git", "diff", ".env.overrides"],
        cwd=str(root), capture_output=True, text=True
    )
    if diff.stdout.strip():
        print(diff.stdout)
    else:
        # 새 파일이거나 untracked
        print(subprocess.run(
            ["git", "status", "--short", ".env.overrides"],
            cwd=str(root), capture_output=True, text=True
        ).stdout)
    print("─────────────────────────────────────────────")

    # 커밋 여부 선택
    ans = input("\n위 내용으로 git commit + push 하시겠습니까? [y/N] ").strip().lower()
    if ans == "y":
        subprocess.run(["git", "add", ".env.overrides"], cwd=str(root))
        subprocess.run(
            ["git", "commit", "-m", "config: 파이 .env.overrides 동기화"],
            cwd=str(root)
        )
        subprocess.run(["git", "push", "origin", "main"], cwd=str(root))
        print("[완료] git push 완료")
    else:
        print("[스킵] 커밋하지 않았습니다. 백테스트 후 필요하면 수동으로 커밋하세요.")

if __name__ == "__main__":
    main()
