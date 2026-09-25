# -*- coding: utf-8 -*-
"""스윙봇 야간 배치 (명세 13절 2·3·4, 11절).

    유니버스 갱신 → 당일 일봉·수급·프로그램 증분 수집(예산 내) → 지수
    → 당일 완결 확인(90% 미만이면 스캔 안 하고 fail 기록)
    → 일봉 스캔·감시 리스트 저장 (DART 재무는 주간 배치 swing_dart_weekly.py 로 분리)
    → runs(date,'nightly') 에 ok/fail 기록. fail 이면 다음날 live 가 신규 진입을 안 한다.

    fail 은 다음 거래일 신규 진입 0건을 뜻하므로 같은 프로세스 안에서 재시도한다.
    수집은 증분이라 이미 받은 종목은 API 호출 없이 건너뛴다 — 재시도가 거의 무료다.
    --stop-after-sec 안에서만 재시도하고, 그 뒤엔 fail 로 남긴다(다음날 live 08:50 은 침범 안 함).

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
    # 실패 시 같은 프로세스에서 재시도. 15:45 시작 + 12h = 03:45 → live 08:50 전에 반드시 끝난다.
    ap.add_argument("--retries", type=int, default=int(os.environ.get("SWING_NIGHTLY_RETRIES", "2")))
    ap.add_argument("--retry-wait-sec", type=int, default=int(os.environ.get("SWING_NIGHTLY_RETRY_WAIT", "900")))
    ap.add_argument("--stop-after-sec", type=int, default=int(os.environ.get("SWING_NIGHTLY_STOP_AFTER", "43200")))
    a = ap.parse_args()
    c = cfg()
    store.init_db(c.db_path)
    date = a.date
    # 휴장일은 새 일봉이 없다 — 수집·스캔을 돌리면 3.5h 를 버리고 fail 로 남아
    # 다음 거래일 live 가 감시 리스트를 못 쓴다. 대장주봇과 같은 판정 모듈을 쓴다.
    # 판정 자체가 실패하면 거래일로 간주한다 — 수집을 거르는 쪽이 더 위험하다.
    try:
        from stock_bot.live.runner import _is_trading_day  # noqa: WPS433
        trading = _is_trading_day(datetime.strptime(date, "%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        logger.warning("nightly {} 휴장일 판정 실패 — 거래일로 간주하고 진행: {}", date, e)
        trading = True
    if not trading:
        logger.info("nightly {} — 휴장일, 수집·스캔 생략", date)
        store.mark_run(date, "nightly", "skip", "휴장일")
        store.close()
        return 0
    t0 = time.time()

    def attempt(budget: int) -> tuple[bool, list[str]]:
        """수집 → 완결 확인 → 스캔 1회. (성공여부, 상세) 반환. 예외는 안 던진다."""
        detail: list[str] = []
        try:
            if not a.no_collect:
                n_u = collector.refresh_universe()
                detail.append(f"universe={n_u}")
                codes = store.all_codes()
                r = collector.collect_daily(codes, budget_sec=budget,
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
            return True, detail
        except Exception as e:  # noqa: BLE001
            detail.append(f"ERR={type(e).__name__}: {e}")
            logger.exception("nightly {} 시도 실패: {}", date, e)
            return False, detail

    try:
        for i in range(max(a.retries, 0) + 1):
            left = a.stop_after_sec - (time.time() - t0)
            # 재시도는 남은 시간 안에서만 수집한다 — 증분이라 대개 남은 종목만 받는다.
            budget = a.budget_sec if i == 0 else max(int(min(a.budget_sec, left)), 60)
            ok, detail = attempt(budget)
            if i:
                detail.append(f"재시도{i}회")
            detail.append(f"{time.time() - t0:.0f}s")
            if ok:
                store.mark_run(date, "nightly", "ok", " | ".join(detail))
                logger.info("nightly {} ok: {}", date, " | ".join(detail))
                return 0
            # 실패는 즉시 남긴다 — 재시도 중에 컨테이너가 죽어도 fail 이 보인다.
            # 재시도가 성공하면 INSERT OR REPLACE 로 ok 가 덮는다.
            store.mark_run(date, "nightly", "fail", " | ".join(detail))
            left = a.stop_after_sec - (time.time() - t0)
            if i >= max(a.retries, 0) or left < a.retry_wait_sec + 300:
                logger.error("nightly {} fail (시도 {}회, 남은예산 {:.0f}s): {}",
                             date, i + 1, left, " | ".join(detail))
                return 1
            logger.warning("nightly {} 실패 — {}s 뒤 재시도 ({}/{})",
                           date, a.retry_wait_sec, i + 1, a.retries)
            time.sleep(a.retry_wait_sec)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
