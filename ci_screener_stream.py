#!/usr/bin/env python3
"""CI(GitHub Actions)에서 screener.py 를 실행하고 stdout 을 GitHub Gist 에 실시간 append.

파이는 LAN-only(인바운드 불가)라 터널 없이 실시간 로그를 받으려면 파이가 아웃바운드로
중계소를 폴링해야 한다. 그 중계소로 GitHub Gist 를 쓴다:
  파이가 빈 gist 생성 → gist_id 를 workflow input 으로 전달 →
  이 스크립트가 screener.py stdout 을 gist 파일에 누적 PATCH →
  파이가 gist 를 ~10초 폴링해 새 내용을 로컬 실행과 동일한 consumer 로 흘린다.

Gist PATCH 는 파일 전체 내용을 교체하므로(append API 없음) 누적 텍스트를 통째로 보낸다.
파이가 content[_consumed:] 로 증분만 소비하도록 내용은 항상 단조 증가해야 하며, 그래서
버퍼는 파이가 gist 생성 시 넣는 것과 **동일한 헤더**(_HEADER)로 시작한다(접두사 불변 보장).
라인 순서는 단일 sender 스레드로 보장. 종료는 마지막 줄 센티넬로 알린다:
  __SCREENER_CI_DONE__ rc=<int>

인증: gist 는 계정 단위라 CI 기본 GITHUB_TOKEN 으론 못 쓴다. gist 권한 PAT 를
      레포 시크릿 SCREENER_GIST_TOKEN 으로 주입한다(파이 SCREENER_GH_TOKEN 과 동일값 가능).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

import requests

# 파이 _gist_create 가 넣는 헤더와 **반드시 동일**해야 한다(접두사 불변 → 파이 증분 소비).
_HEADER = "[CI 스코어링 로그]\n"
_DONE_SENTINEL = "__SCREENER_CI_DONE__"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gist-id", required=True, help="파이가 생성한 중계 gist ID")
    ap.add_argument("--gist-file", default="screener.log", help="gist 내 로그 파일명")
    ap.add_argument("--market", default="all")
    ap.add_argument("--market-top", default="800")
    ap.add_argument("--top", default="2")
    ap.add_argument("--workers", default="2")
    ap.add_argument("--sector", default="")
    a = ap.parse_args()

    token = os.environ.get("SCREENER_GIST_TOKEN", "")
    api = f"https://api.github.com/gists/{a.gist_id}"
    sess = requests.Session()
    hdr = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def patch(content: str, retries: int = 4) -> bool:
        last = ""
        for i in range(retries):
            try:
                r = sess.patch(
                    api, json={"files": {a.gist_file: {"content": content}}},
                    headers=hdr, timeout=30,
                )
                if r.status_code == 200:
                    return True
                last = f"HTTP {r.status_code} {r.text[:120]}"
                # 403/429 rate limit 이면 재시도 폭주가 오히려 한도를 더 태운다.
                # 즉시 포기하고 다음 sender 사이클(간격 뒤)에 맡긴다 — 그동안 잔여
                # 라인은 버퍼에 쌓여있다 통째로 다음 PATCH 에 실린다(유실 없음).
                if r.status_code in (403, 429) and "rate limit" in r.text.lower():
                    sys.stderr.write(f"[ci-gist] rate limit — 이번 PATCH 스킵: {last}\n")
                    sys.stderr.flush()
                    return False
            except Exception as e:  # noqa: BLE001 — 네트워크 순단 재시도
                last = repr(e)
            time.sleep(0.8 * (i + 1))
        sys.stderr.write(f"[ci-gist] PATCH 실패: {last}\n")
        sys.stderr.flush()
        return False

    here = os.path.dirname(os.path.abspath(__file__))
    cmd = [
        sys.executable, os.path.join(here, "screener.py"),
        "--mode", "weekly",
        "--market", a.market,
        "--market-top", str(a.market_top),
        "--top", str(a.top),
        "--dry-run",                       # 파이 로컬 경로와 동일: CI는 스코어링만, SYMBOLS 기록은 파이가
        "--workers", str(a.workers),
    ]
    if a.sector:
        cmd += ["--sector", a.sector]

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("MALLOC_ARENA_MAX", "2")

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, text=True, encoding="utf-8", errors="replace", env=env,
    )
    assert proc.stdout is not None

    lock = threading.Lock()
    buf_parts: list[str] = [_HEADER]   # 파이 헤더와 동일 접두사로 시작(단조 증가 보장)
    finished = threading.Event()

    def reader() -> None:
        # screener.py stdout → CI 자체 로그 미러 + 누적 버퍼(순서 유지)
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            with lock:
                buf_parts.append(line)   # line 은 개행 포함
        finished.set()

    def sender() -> None:
        # 유일한 PATCH 발신자 — 누적 전체를 통째 교체(라인 뒤섞임 없음).
        # 간격 20초: gist PATCH 는 raw 로 못 바꿔 반드시 api.github.com(사용자당 5000/hr
        #   한도 카운트). 잡 타임아웃 30→120분으로 늘며 PATCH 총량이 10초면 ~720회/런까지
        #   → 20초로 절반(~360회). 백그라운드 스코어링이라 20초 지연 무해. 종료 시 최종
        #   PATCH 는 즉시(아래) 나가 완료감지는 안 늦는다. 못 보낸 라인은 다음 PATCH 에 통째로.
        last_sent = ""
        while not finished.is_set():
            time.sleep(20.0)
            with lock:
                content = "".join(buf_parts)
            if content != last_sent and patch(content):
                last_sent = content

    rt = threading.Thread(target=reader, daemon=True)
    st = threading.Thread(target=sender, daemon=True)
    rt.start()
    st.start()
    rt.join()
    st.join()
    proc.wait()

    # 종료 센티넬 append + 최종 PATCH(그동안 못 보낸 잔여 라인까지 통째 포함).
    with lock:
        final = "".join(buf_parts) + f"{_DONE_SENTINEL} rc={proc.returncode}\n"
    patch(final)
    return 0 if proc.returncode == 0 else int(proc.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
