# -*- coding: utf-8 -*-
"""스윙봇 야간 배치 (명세 13절 2·3·4, 11절).

    유니버스 갱신 → 당일 일봉·수급·프로그램 증분 수집(예산 내) → 지수
    → 당일 완결 확인(90% 미만이면 스캔 안 하고 fail 기록)
    → 일봉 스캔·감시 리스트 저장 (DART 재무는 주간 배치 swing_dart_weekly.py 로 분리)
    → runs(date,'nightly') 에 ok/fail 기록. fail 이면 다음날 live 가 신규 진입을 안 한다.

    사용:  python scripts/swing_nightly.py [--date YYYYMMDD] [--budget-sec N] [--no-collect]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from loguru import logger  # noqa: E402

from stock_bot.swing import collector, daily_scan, store  # noqa: E402
from stock_bot.swing.config import cfg  # noqa: E402


def main() -> int:
    # swing-bot 컨테이너(./logs:/app/logs) 에서는 live 스윙과 같은 파일에 기록 → 웹 로그탭 '스윙봇'
    if Path("/app/logs").is_dir():
        logger.add("/app/logs/stock_swing.log", rotation="10 MB", retention=10, buffering=1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    # 모의키 1건/s × 종목당 3콜(일봉·수급·프로그램) ≈ 4s/종목 → 2,765종목 ≈ 3.1h. 3h 예산은 9/21 128종목 미수집.
    ap.add_argument("--budget-sec", type=int, default=int(os.environ.get("SWING_NIGHTLY_BUDGET_SEC", "14400")))
    ap.add_argument("--no-collect", action="store_true", help="수집 생략, 스캔만")
    ap.add_argument("--min-ratio", type=float, default=0.9)
    a = ap.parse_args()
    c = cfg()
    store.init_db(c.db_path)
    date = a.date
    # 휴장일은 새 일봉이 없다 — 수집·스캔을 돌리면 fail 로 남아 다음 거래일 live 가
    # 감시 리스트를 못 쓴다. 대장주봇과 같은 판정 모듈을 쓴다.
    from stock_bot.live.runner import _is_trading_day  # noqa: WPS433
    if not _is_trading_day(datetime.strptime(date, "%Y%m%d")):
        logger.info("nightly {} — 휴장일, 수집·스캔 생략", date)
        store.mark_run(date, "nightly", "skip", "휴장일")
        store.close()
        return 0
    t0 = time.time()
    detail: list[str] = []
    try:
        if not a.no_collect:
            n_u = collector.refresh_universe()
            detail.append(f"universe={n_u}")
            codes = store.all_codes()
            r = collector.collect_daily(codes, budget_sec=a.budget_sec,
                                        with_program=c.collect_program)
            detail.append(f"collect ok={r['ok']} fail={r['fail']} skipped={r['skipped']}")
            n_i = collector.collect_index()
            detail.append(f"index+{n_i}")
        ok, msg = collector.check_today_complete(date, a.min_ratio)
        detail.append(f"complete={msg}")
        if not ok:
            raise RuntimeError(f"당일 일봉 미완결 {msg} — 스캔 생략")
        sig, wl = daily_scan.run_nightly_scan(date)
        detail.append(f"signals={len(sig)} watch={len(wl)}")
        detail.append(f"{time.time() - t0:.0f}s")
        store.mark_run(date, "nightly", "ok", " | ".join(detail))
        logger.info("nightly {} ok: {}", date, " | ".join(detail))
        return 0
    except Exception as e:  # noqa: BLE001
        detail.append(f"ERR={type(e).__name__}: {e}")
        store.mark_run(date, "nightly", "fail", " | ".join(detail))
        logger.exception("nightly {} fail: {}", date, e)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
