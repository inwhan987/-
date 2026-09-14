# -*- coding: utf-8 -*-
"""DART 재무 주간 전종목 배치 (SWING_TASK_AXIS_SCORE.md 1-2). 기록용 — 전략 판단에 안 쓴다.

    python scripts/swing_dart_weekly.py [--budget-sec 3600] [--force] [--date YYYYMMDD]

  대상: store.all_codes() 전체(유니버스). dart.collect() 재사용 — 30일 캐시 안이면 API 안 부름.
  예산(초)을 넘기면 나머지는 끊고 runs(date,'dart_weekly') 에 기록한다. 캐시 덕에 다음 실행이
  이어받는다. DART 일일 한도(status 020)면 즉시 중단·fail 기록 (dart.DartError 경로).
  SWING_DART_ENABLED=false 면 아무것도 안 한다 (--force 는 스위치·캐시 둘 다 무시).
  DART_API_KEY 는 환경변수로만 읽고 로그·DB 에 남기지 않는다.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from loguru import logger  # noqa: E402

from stock_bot.swing import dart, store  # noqa: E402
from stock_bot.swing.config import cfg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-sec", type=int, default=None,
                    help="시간 예산(초). 기본 SWING_DART_BUDGET_SEC")
    ap.add_argument("--force", action="store_true", help="스위치·캐시 무시하고 전부 다시 받음")
    ap.add_argument("--date", default=datetime.now().strftime("%Y%m%d"), help="dart_fin.date 에 적을 날짜")
    a = ap.parse_args()
    c = cfg()
    budget = c.dart_budget_sec if a.budget_sec is None else a.budget_sec
    if not c.dart_enabled and not a.force:
        logger.info("SWING_DART_ENABLED=false — 주간 배치 건너뜀 (--force 로 강제)")
        return 0
    store.init_db(c.db_path)
    t0 = time.time()
    try:
        codes = store.all_codes()
        logger.info("DART 주간 배치 {}종목 예산 {}s force={}", len(codes), budget, a.force)
        n = dart.collect(codes, a.date, budget_sec=budget, force=a.force)
        n_rc = store.conn().execute(
            "SELECT COUNT(*) FROM dart_fin WHERE date=? AND rcept_dt IS NOT NULL", (a.date,)).fetchone()[0]
        detail = f"{n} | rcept_dt={n_rc} | {time.time() - t0:.0f}s"
        status = "partial" if n["skipped"] else "ok"
        store.mark_run(a.date, "dart_weekly", status, detail)
        logger.info("dart_weekly {} {}: {}", a.date, status, detail)
        return 2 if n["skipped"] else 0
    except dart.DartError as e:
        store.mark_run(a.date, "dart_weekly", "fail", f"ERR={e} | {time.time() - t0:.0f}s")
        logger.error("dart_weekly {} fail: {}", a.date, e)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
