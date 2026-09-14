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
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--budget-sec", type=int, default=int(os.environ.get("SWING_NIGHTLY_BUDGET_SEC", "10800")))
    ap.add_argument("--no-collect", action="store_true", help="수집 생략, 스캔만")
    ap.add_argument("--min-ratio", type=float, default=0.9)
    a = ap.parse_args()
    c = cfg()
    store.init_db(c.db_path)
    date = a.date
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
