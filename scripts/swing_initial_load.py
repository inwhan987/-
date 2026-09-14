# -*- coding: utf-8 -*-
"""스윙봇 초기 적재 (명세 13절 3). 장 끝나고(15:30 이후) 한 번 돌린다.

    1) 유니버스(pykrx) → meta
    2) KIS 일봉 400봉 + 수급 30일 + 당일 밸류 + 프로그램 60일   (모의키 1/s ≈ 5.5h, 실전 데이터키 ≈ 8분)
    3) KOSPI 지수 400봉
    4) 수급·PER/PBR 과거분 400일 — bt_swing.data 의 pykrx 수집기 (rps 0.7, 캐시 재사용)

중단돼도 다시 돌리면 이어서 한다(daily 는 마지막 날짜 이후 증분, pykrx 는 flow 가
이미 있는 종목 건너뜀). KRX 차단(Blocked)이면 즉시 멈춘다 — 우회하지 않는다.

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
            # KIS 가 30일치는 넣어 뒀으므로 '과거분' 이 없는 종목 = flow 첫 날짜가 최근인 종목
            first = {r[0]: r[1] for r in store.conn().execute(
                "SELECT code, MIN(date) FROM flow GROUP BY code")}
            cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")
            todo = [cd for cd in codes if not first.get(cd) or first[cd] > cutoff]
            logger.info("pykrx 과거분 대상 {}종목 ({} ~ {})", len(todo), start, end)
            r = collector.load_history_pykrx(todo, start, end, rps=a.pykrx_rps)
            logger.info("pykrx ok={} fail={} {:.0f}s", r["ok"], r["fail"], time.time() - t0)
        logger.info("초기 적재 완료 {:.0f}s", time.time() - t0)
        return 0
    except collector.Blocked as e:
        logger.error("중단: {}", e)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
