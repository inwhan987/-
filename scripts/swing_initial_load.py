# -*- coding: utf-8 -*-
"""스윙봇 초기 적재 (명세 13절 3). 장 끝나고(15:30 이후) 한 번 돌린다.

    1) 유니버스(pykrx) → meta
    2) KIS 일봉 400봉 + 수급 30일 + 당일 밸류 + 프로그램 60일   (모의키 1/s ≈ 5.5h, 실전 데이터키 ≈ 8분)
    3) KOSPI 지수 400봉
    4) 수급·PER/PBR 과거분 — bt_swing 캐시 구간은 파일, 나머지는 pykrx 날짜 루프(전종목 일괄, rps 0.7)

중단돼도 다시 돌리면 이어서 한다(daily 는 마지막 날짜 이후 증분, pykrx 는 날짜별
캐시·이미 채워진 날짜 건너뜀). KRX 차단(Blocked)이면 즉시 멈춘다 — 우회하지 않는다.

    python scripts/swing_initial_load.py [--count 400] [--budget-sec N] [--skip-kis] [--skip-pykrx]
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from loguru import logger  # noqa: E402

from stock_bot.swing import collector, store  # noqa: E402
from stock_bot.swing.config import cfg  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=400)
    ap.add_argument("--budget-sec", type=int, default=0, help="KIS 수집 시간 예산(0=무제한)")
    ap.add_argument("--rps", type=float, default=None, help="KIS 초당 호출(기본 settings)")
    ap.add_argument("--pykrx-rps", type=float, default=0.7)
    ap.add_argument("--skip-kis", action="store_true")
    ap.add_argument("--skip-pykrx", action="store_true")
    a = ap.parse_args()
    if a.pykrx_rps > 0.7:
        ap.error("--pykrx-rps 는 0.7 이하")
    c = cfg()
    store.init_db(c.db_path)
    t0 = time.time()
    try:
        n_u = collector.refresh_universe()
        codes = store.all_codes()
        logger.info("universe {}종목", n_u)

        if not a.skip_kis:
            b = collector.DataBroker(rps=a.rps)
            try:
                r = collector.collect_daily(codes, count=a.count, budget_sec=a.budget_sec,
                                            with_program=c.collect_program, broker=b)
                logger.info("KIS 일봉 ok={} fail={} skipped={} {:.0f}s", r["ok"], r["fail"],
                            r["skipped"], time.time() - t0)
                if r["skipped"]:
                    logger.warning("예산 초과로 {}종목 미수집 — 다시 실행하면 이어서 한다", r["skipped"])
                    return 2
                n_i = collector.collect_index(count=a.count, broker=b)
                logger.info("지수 +{}봉", n_i)
            finally:
                b.close()

        if not a.skip_pykrx:
            end = datetime.now().strftime("%Y%m%d")
            start = (datetime.now() - timedelta(days=int(a.count * 1.6) + 10)).strftime("%Y%m%d")
            # 날짜 루프: bt_swing 캐시 구간은 파일에서, 나머지 영업일은 전종목 일괄 함수로.
            # 이미 90% 이상 들어 있는 날짜(KIS 30일치 등)는 건너뛴다.
            logger.info("pykrx 과거분 {}종목 ({} ~ {})", len(codes), start, end)
            r = collector.load_history_pykrx(codes, start, end, rps=a.pykrx_rps)
            logger.info("pykrx 캐시 {} 적중 {} 미스 {} | 날짜 {} (신규 {} 캐시 {} 건너뜀 {} 실패 {}) "
                        "콜 {} | flow {}행 fund {}행 {:.0f}s", r["cache_range"], r["cache_hit"],
                        r["cache_miss"], r["dates_total"], r["dates_net"], r["dates_cached"],
                        r["dates_skipped"], len(r["fail_dates"]), r["calls"], r["rows_flow"],
                        r["rows_fund"], r["sec"])
            if r["fail_dates"]:
                logger.warning("pykrx 실패 날짜(다음 실행에 재시도): {}", r["fail_dates"])
        logger.info("초기 적재 완료 {:.0f}s", time.time() - t0)
        return 0
    except collector.Blocked as e:
        logger.error("중단: {}", e)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
