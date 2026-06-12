"""대장주 눌림목 전략 전용 러너 (leader-bot 컨테이너).

기존 앙상블 러너(run_live)와 프로세스를 분리해 운용한다:
  · 이 프로세스 장애·재시작이 메인 봇에 영향 없음 (자본·전략·로그 모두 분리)
  · 9:30~13:00 10분 간격 대장주 선별(leader_finder) + 평일 장중 매분 매매 tick
  · KIS 토큰은 .kis_tokens 파일 캐시를 메인 봇·웹과 공유 (재발급 충돌 없음)

스케줄 판정 헬퍼(_is_trading_day 등)와 env 핫리로드 워처는 runner 모듈 것을
그대로 재사용한다 — _HOT_FIELDS 의 LEADER_* 키도 동일하게 반영된다.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, time as dtime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

import stock_bot.live.runner as _runner
from stock_bot.broker import KISBroker
from stock_bot.config import settings
from stock_bot.live.leader_trader import LeaderTrader
from stock_bot.live.runner import _is_market_open, _is_trading_day, _start_env_watcher
from stock_bot.market_calendar import KST as _KST
from stock_bot.notify import notify
from stock_bot.storage import init_db

_ROOT = Path(__file__).resolve().parents[2]


def run_leader() -> None:
    init_db()
    _start_env_watcher("leader")
    broker = KISBroker()
    _runner._holiday_broker = broker  # 휴장일 판정을 KIS 달력 기준으로

    mode = "시뮬레이션" if settings.trade_dry_run else (
        "실전" if settings.kis_env == "real" else "모의투자"
    )
    notify(
        f"👑 **대장주봇 기동** [{mode}]\n"
        f"매매 {'ON' if settings.leader_trade_enabled else 'OFF'} · "
        f"예산 {settings.leader_budget_krw:,.0f}원 · {settings.leader_interval_min}분봉 · "
        f"손절 -{settings.leader_stop_buf_pct:g}% / 익절 +{settings.leader_tp_pct:g}%"
    )
    logger.info(
        "leader runner started (trade_enabled={}, budget={:,.0f})",
        settings.leader_trade_enabled, settings.leader_budget_krw,
    )

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # ── 대장주 선별 (테마 모드): 9:30 첫 시도 → 미선별 시 10분 간격, 13:00 마지막 ──
    # 선별 성공(data/leader_picks/날짜.json 생성) 시 그날은 중지.
    # 디스코드 알림은 leader_finder.py 가 매 시도마다 직접 발송('없음' 포함).
    def _leader_pick_tick():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        t = now.time()
        if t < dtime(9, 30) or t > dtime(13, 0):
            return
        picks = _ROOT / "data" / "leader_picks" / f"{now:%Y-%m-%d}.json"
        if picks.exists():
            return  # 오늘 선별 완료 → 종료
        cmd = [sys.executable, str(_ROOT / "leader_finder.py"),
               "--once", "--theme", "--summary-only"]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=540, cwd=str(_ROOT),
            )
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            logger.info(
                "leader pick [{:%H:%M}] {} (exit={}) {}",
                now,
                "선별 완료 — 오늘 스케줄 종료" if picks.exists() else "미선별 — 10분 후 재시도",
                r.returncode,
                tail[-1] if tail else "",
            )
        except subprocess.TimeoutExpired:
            logger.warning("leader pick 타임아웃 (540초) — 다음 회차에 재시도")
        except Exception as e:
            logger.warning("leader pick 실패: {}", e)

    scheduler.add_job(
        _leader_pick_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*/10"),
        id="leader_pick",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader pick scheduled: mon-fri 9:30 → 10min retry until 13:00 (theme mode)")

    # ── 눌림목 매매: 평일 장중 매분 tick (LEADER_TRADE_ENABLED 로 on/off) ──
    leader_trader = LeaderTrader(broker)

    def _leader_trade_tick():
        if not settings.leader_trade_enabled:
            return
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now) or not _is_market_open(now):
            return
        try:
            leader_trader.tick()
        except Exception as e:
            logger.exception("leader_trader tick 실패: {}", e)

    scheduler.add_job(
        _leader_trade_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*"),
        id="leader_trade",
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "leader trade scheduled: mon-fri 9-15 every minute (enabled={})",
        settings.leader_trade_enabled,
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("leader runner shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_leader()
