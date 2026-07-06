#!/usr/bin/env python3
"""CI(GitHub Actions)에서 screener.py 를 실행하고 stdout 을 파이 웹(터널)로 실시간 스트리밍.

파이의 로컬 실행 경로(_run_sc_job 의 reader thread)와 100% 동일하게 저장/파싱되도록,
screener.py 의 stdout 을 **가공 없이 라인 그대로** 파이 ingest 엔드포인트로 보낸다.
파이 쪽 consumer 가 로컬 실행과 똑같이 _captured_lines/_SC_STREAM_BUF/_file_append 로 흘린다.

라인 순서 보장이 중요하다(파이가 "".join 후 정규식으로 "선별 N개"·SCREENER_JSON 파싱).
그래서 라인 POST 는 **단일 sender 스레드**만 수행해 배치들이 절대 뒤섞이지 않게 하고,
done 신호는 sender 종료 후 메인이 마지막에 한 번 보낸다.

전송 프로토콜 (JSON POST → {callback}/api/screener/ingest, 헤더 X-Ingest-Secret):
  진행 : {"token": <run_token>, "lines": ["...", "..."]}   # 개행 제거된 라인들
  종료 : {"token": <run_token>, "done": true, "returncode": <int>}
"""
from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import threading
import time

import requests

_DONE = object()   # reader → sender 종료 센티넬


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--callback", required=True, help="파이 터널 베이스 URL (예: https://xxx.trycloudflare.com)")
    ap.add_argument("--token", required=True, help="이번 실행 nonce (파이가 발급, ingest 매칭용)")
    ap.add_argument("--market", default="all")
    ap.add_argument("--market-top", default="800")
    ap.add_argument("--top", default="2")
    ap.add_argument("--workers", default="2")
    ap.add_argument("--sector", default="")
    a = ap.parse_args()

    ingest = a.callback.rstrip("/") + "/api/screener/ingest"
    secret = os.environ.get("SCREENER_CI_INGEST_SECRET", "")
    sess = requests.Session()
    # ngrok-skip-browser-warning: ngrok 무료 플랜이 끼우는 브라우저 경고 인터스티셜을
    #   무력화(안 그러면 파이가 JSON 대신 HTML 경고를 받을 수 있음). cloudflared 등
    #   다른 터널엔 무해한 잉여 헤더라 항상 붙여도 안전.
    headers = {"X-Ingest-Secret": secret, "ngrok-skip-browser-warning": "true"}

    def post(payload: dict, retries: int = 4) -> bool:
        last = ""
        for i in range(retries):
            try:
                r = sess.post(ingest, json=payload, headers=headers, timeout=20)
                if r.status_code == 200:
                    return True
                last = f"HTTP {r.status_code} {r.text[:120]}"
            except Exception as e:  # noqa: BLE001 — 네트워크/터널 순단 재시도
                last = repr(e)
            time.sleep(0.8 * (i + 1))
        sys.stderr.write(f"[ci-stream] ingest POST 실패: {last}\n")
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

    q: "queue.Queue" = queue.Queue()

    def reader() -> None:
        # screener.py stdout → CI 자체 로그 미러 + 큐 적재 (순서 유지)
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            q.put(line.rstrip("\n"))
        q.put(_DONE)

    def sender() -> None:
        # 유일한 POST 발신자 — 배치들이 뒤섞이지 않도록 단일 스레드로만 전송.
        batch: list[str] = []
        last = time.time()
        done = False
        while not done:
            try:
                item = q.get(timeout=0.3)
                if item is _DONE:
                    done = True
                else:
                    batch.append(item)
            except queue.Empty:
                pass
            if batch and (done or len(batch) >= 40 or (time.time() - last) >= 0.4):
                post({"token": a.token, "lines": batch})
                batch = []
                last = time.time()

    rt = threading.Thread(target=reader, daemon=True)
    st = threading.Thread(target=sender, daemon=True)
    rt.start()
    st.start()
    rt.join()
    st.join()                               # 모든 라인 배치 전송 완료 보장

    proc.wait()
    post({"token": a.token, "done": True, "returncode": proc.returncode})
    return 0 if proc.returncode == 0 else int(proc.returncode or 1)


if __name__ == "__main__":
    raise SystemExit(main())
