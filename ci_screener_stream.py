#!/usr/bin/env python3
"""CI(GitHub Actions)에서 screener.py 를 실행하고 stdout 을 파이로 실시간 중계.

파이는 LAN-only(인바운드 불가)라 두 가지 전송 방식을 지원한다 — 어느 쪽이든 CI는 항상
헤더로 시작하는 **누적 전체 로그**를 흘리고, 파이는 content[_consumed:] 증분만 소비한다:

  (A) 터널 push(--callback-url 지정 시, 기본): 파이가 cloudflared quick tunnel 로 뚫은
      인바운드 URL 을 디스패치 때 넘겨준다. 이 스크립트가 누적 전체를 그 URL 의
      /api/screener/ingest 로 직접 POST(공유 시크릿 헤더) → 파이가 즉시 소비.
      GitHub API 한도와 완전 무관하고 실시간(폴링 지연 없음).
  (B) gist 폴백(--gist-id 지정, callback 빈 값): 파이가 만든 비공개 gist 에 누적 전체를
      PATCH → 파이가 raw_url 폴링. 터널이 없을 때만 쓴다.

PATCH/POST 모두 전체 교체(append 아님)라 누적 텍스트를 통째로 보낸다. 내용은 항상 단조
증가해야 하며 파이 버퍼와 **동일한 헤더**(_HEADER)로 시작한다(접두사 불변 보장). 라인
순서는 단일 sender 스레드로 보장. 종료는 마지막 줄 센티넬로 알린다:
  __SCREENER_CI_DONE__ rc=<int>

인증: (A) 파이 .env 와 레포 시크릿에 동일 SCREENER_CI_INGEST_SECRET → X-Ingest-Secret 헤더.
      (B) gist 는 계정 단위라 기본 GITHUB_TOKEN 불가 → gist 권한 PAT SCREENER_GIST_TOKEN.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time

import requests

# 파이 버퍼가 시작하는 헤더와 **반드시 동일**해야 한다(접두사 불변 → 파이 증분 소비).
_HEADER = "[CI 스코어링 로그]\n"
_DONE_SENTINEL = "__SCREENER_CI_DONE__"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gist-id", default="", help="중계 gist ID(gist 폴백 모드)")
    ap.add_argument("--gist-file", default="screener.log", help="gist 내 로그 파일명")
    ap.add_argument("--callback-url", default="", help="파이 터널 base URL(터널 push 모드)")
    ap.add_argument("--run-token", default="", help="실행 nonce(ingest 라우팅용)")
    ap.add_argument("--market", default="all")
    ap.add_argument("--market-top", default="800")
    ap.add_argument("--top", default="2")
    ap.add_argument("--workers", default="2")
    ap.add_argument("--sector", default="")
    a = ap.parse_args()

    sess = requests.Session()

    # 전송 모드 결정 — callback-url 이 있으면 터널 push, 아니면 gist 폴백.
    push_mode = bool((a.callback_url or "").strip())
    ingest_url = (a.callback_url or "").rstrip("/") + "/api/screener/ingest"
    ingest_secret = os.environ.get("SCREENER_CI_INGEST_SECRET", "")

    token = os.environ.get("SCREENER_GIST_TOKEN", "")
    api = f"https://api.github.com/gists/{a.gist_id}"
    hdr = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def post_pi(content: str, retries: int = 4) -> bool:
        """터널 push — 누적 전체를 파이 ingest 로 POST. 실패해도 잔여는 다음 사이클에
        통째로 재전송(누적 전체라 유실 없음). 410(파이가 run 종료/취소) 이면 조용히 포기."""
        last = ""
        for i in range(retries):
            try:
                r = sess.post(
                    ingest_url,
                    json={"run_token": a.run_token, "content": content},
                    headers={"X-Ingest-Secret": ingest_secret},
                    timeout=30,
                )
                if r.status_code == 200:
                    return True
                if r.status_code == 410:   # 파이가 이미 종료/취소 — 더 보낼 필요 없음
                    return False
                last = f"HTTP {r.status_code} {r.text[:120]}"
            except Exception as e:  # noqa: BLE001 — 터널 순단 재시도
                last = repr(e)
            time.sleep(0.8 * (i + 1))
        sys.stderr.write(f"[ci-ingest] POST 실패(다음 사이클 재전송): {last}\n")
        sys.stderr.flush()
        return False

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

    # 전송 함수·간격 — push 는 API 한도 무관이라 짧게(2초, 실시간). gist 는 한도 절약 20초.
    send = post_pi if push_mode else patch
    interval = 2.0 if push_mode else 20.0
    sys.stderr.write(
        f"[ci-relay] mode={'push' if push_mode else 'gist'} interval={interval}s\n")
    sys.stderr.flush()

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
        # 유일한 발신자 — 누적 전체를 통째 교체 전송(라인 뒤섞임 없음). 못 보낸 라인은
        # 다음 사이클에 통째로 재전송되므로 순단/스킵에도 유실이 없다. 종료 시 최종
        # 전송은 즉시(아래) 나가 완료감지는 안 늦는다.
        last_sent = ""
        while not finished.is_set():
            time.sleep(interval)
            with lock:
                content = "".join(buf_parts)
            if content != last_sent and send(content):
                last_sent = content

    rt = threading.Thread(target=reader, daemon=True)
    st = threading.Thread(target=sender, daemon=True)
    rt.start()
    st.start()
    rt.join()
    st.join()
    proc.wait()

    # 종료 센티넬 append + 최종 전송(그동안 못 보낸 잔여 라인까지 통째 포함).
    with lock:
        final = "".join(buf_parts) + f"{_DONE_SENTINEL} rc={proc.returncode}\n"
    send(final)
    return 0 if proc.returncode == 0 else int(proc.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
