"""대장주 눌림목 전략 전용 러너 (leader-bot 컨테이너).

기존 앙상블 러너(run_live)와 프로세스를 분리해 운용한다:
  · 이 프로세스 장애·재시작이 메인 봇에 영향 없음 (자본·전략·로그 모두 분리)
  · 9:30~13:00 10분 간격 대장주 선별(leader_finder) + 평일 장중 매분 매매 tick
  · KIS 토큰은 .kis_tokens 파일 캐시를 메인 봇·웹과 공유 (재발급 충돌 없음)

스케줄 판정 헬퍼(_is_trading_day 등)와 env 핫리로드 워처는 runner 모듈 것을
그대로 재사용한다 — _HOT_FIELDS 의 LEADER_* 키도 동일하게 반영된다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, time as dtime, timedelta
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


def _selection_args() -> list[str]:
    """대장주 선별 기준을 leader_finder CLI 인자로 변환.

    settings 값(웹 파라미터탭에서 조정·핫리로드)을 그대로 subprocess 에 넘긴다.
    선별 로직(leader_finder)은 무변경 — 임계값만 주입. 정본 선별(--)과 재선별
    (--reval) 모두 동일 인자를 써야 공정 비교가 되므로 한 곳에서 만든다.
    기본값은 leader_finder argparse 기본과 동일 → 값 미변경 시 동작 불변.
    """
    return [
        "--top", str(int(settings.leader_sel_top)),
        "--rise-min", str(float(settings.leader_sel_rise_min)),
        "--hot-min", str(int(settings.leader_sel_hot_min)),
        "--vol-mult", str(float(settings.leader_sel_vol_mult)),
        "--min-value", str(float(settings.leader_sel_min_value_eok)),
        "--dyn-value-pct", str(float(settings.leader_sel_dyn_value_pct)),
        "--mf-clamp-low",    str(float(settings.leader_mf_clamp_low)),
        "--mf-clamp-high",   str(float(settings.leader_mf_clamp_high)),
        "--min-mktcap", str(float(settings.leader_sel_min_cap_eok)),
        "--max-change", str(float(settings.leader_sel_max_change)),
        "--turnover-gate-base",  str(float(settings.leader_sel_turnover_gate_base)),
        "--turnover-gate-slope", str(float(settings.leader_sel_turnover_gate_slope)),
        "--turnover-cap-pct",    str(float(settings.leader_sel_turnover_cap_pct)),
    ]


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
        f"매매 {'ON' if settings.leader_trade_enabled else 'OFF·관전(가상매매 로그)'} · "
        f"예산 {settings.leader_budget_krw:,.0f}원 · {settings.leader_interval_min}분봉 · "
        f"손절 -{settings.leader_stop_buf_pct:g}% / 익절 +{settings.leader_tp_pct:g}%"
    )
    logger.info(
        "leader runner started (trade_enabled={}, budget={:,.0f})",
        settings.leader_trade_enabled, settings.leader_budget_krw,
    )

    scheduler = BlockingScheduler(timezone="Asia/Seoul")

    # ── market_flow 백필: 매 영업일 08:30, pykrx KRX-only 로 20일 top-N 거래대금 합
    # 캐시 갱신. 09:28:30 첫 pick tick 전에 완료되어야 배수 계산이 유효해진다.
    def _leader_prefetch_market_flow():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        cmd = [sys.executable, str(_ROOT / "leader_finder.py"),
               "--prefetch-market-flow",
               "--top", str(int(settings.leader_sel_top))]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=180, cwd=str(_ROOT),
            )
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            logger.info(
                "leader market_flow prefetch [{:%H:%M}] (exit={}) {}",
                now, r.returncode, tail[-1] if tail else "",
            )
        except subprocess.TimeoutExpired:
            logger.warning("leader market_flow prefetch 타임아웃 (180초)")
        except Exception as e:
            logger.warning("leader market_flow prefetch 실패: {}", e)

    scheduler.add_job(
        _leader_prefetch_market_flow,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=30),
        id="leader_prefetch_market_flow",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader market_flow prefetch scheduled: mon-fri 08:30 (pykrx KRX × 어제 r, 20d top-N)")

    # ── 최초 실행 자동 백필: 캐시가 20일 미만이면 러너 부팅 직후 1회.
    # 08:30 크론 기다리지 않고 배포 즉시 폴백 baseline 활성화.
    try:
        _mf_cache_path = _ROOT / "data" / "leader_market_flow.json"
        _mf_days_have = 0
        if _mf_cache_path.exists():
            _mf_data = json.loads(_mf_cache_path.read_text(encoding="utf-8"))
            # 2026-08-10: KRX-only 스키마 마커 없으면 구 UN-스케일 캐시로 간주 → 0
            if _mf_data.get("__schema__") == "krx_only_v1":
                _mf_days_have = sum(1 for k, v in _mf_data.items()
                                    if k != "__schema__" and v and float(v) > 0)
        if _mf_days_have < 20:
            logger.info(
                "leader market_flow 캐시 {}/20일 → 부팅 직후 백필 1회 실행",
                _mf_days_have,
            )
            _leader_prefetch_market_flow()
    except Exception as e:
        logger.warning("leader market_flow 부팅 백필 실패: {}", e)

    # ── 대장주 선별 (테마 모드): 9:30 첫 시도 → 미선별 시 10분 간격, 13:00 마지막 ──
    # 선별 성공(data/leader_picks/날짜.json 생성) 시 그날은 중지.
    # 디스코드 알림은 leader_finder.py 가 매 시도마다 직접 발송('없음' 포함).
    def _leader_pick_tick():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        t = now.time()
        # 선별이 ~90초 걸려서 9:28:30 에 시작 → 9:30 에 picks 완성하도록 게이트를 앞당김.
        if t < dtime(9, 28, 30) or t > dtime(13, 0):
            return
        picks = _ROOT / "data" / "leader_picks" / f"{now:%Y-%m-%d}.json"
        if picks.exists():
            return  # 오늘 선별 완료 → 종료
        cmd = [sys.executable, str(_ROOT / "leader_finder.py"),
               "--once", "--theme", "--summary-only", *_selection_args()]
        # 미선별 '없음' 알림은 마지막 시도(13:00 직전)에만 — 그 전 재시도는 억제해
        # 디스코드 스팸 방지. 다음 발화(+10분)가 13:00 을 넘으면 이번이 마지막 시도.
        if (now + timedelta(minutes=10)).time() <= dtime(13, 0):
            cmd.append("--suppress-empty-alert")
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=540, cwd=str(_ROOT),
            )
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            if picks.exists():
                logger.info(
                    "leader pick [{:%H:%M}] 선별 완료 — 오늘 스케줄 종료 (exit={}) {}",
                    now, r.returncode, tail[-1] if tail else "",
                )
            else:
                # 미선별: stdout 에서 "조건 충족 대장주 없음" 이후 진단블록을
                # 통째로 로그에 남긴다(회전율/거래대금 하한·자격통과 몇종목·탈락사유 등).
                _diag_lines: list[str] = []
                _capture = False
                for _ln in tail:
                    if "조건 충족 대장주 없음" in _ln:
                        _capture = True
                    if _capture:
                        _diag_lines.append(_ln.rstrip())
                        if len(_diag_lines) >= 30:
                            break
                _diag_block = "\n".join(_diag_lines) if _diag_lines else (tail[-1] if tail else "")
                logger.info(
                    "leader pick [{:%H:%M}] 미선별 — 10분 후 재시도 (exit={})\n{}",
                    now, r.returncode, _diag_block,
                )
        except subprocess.TimeoutExpired:
            logger.warning("leader pick 타임아웃 (540초) — 다음 회차에 재시도")
        except Exception as e:
            logger.warning("leader pick 실패: {}", e)

    scheduler.add_job(
        _leader_pick_tick,
        # minute="8-58/10" = 8,18,28,38,48,58 분 + second=30 → :28:30 부터 10분 간격.
        # 9:08:30·9:18:30 발화는 위 게이트(< 9:28:30)가 막아 첫 실행은 9:28:30.
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="8-58/10", second=30),
        id="leader_pick",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader pick scheduled: mon-fri 9:28:30 → 10min retry until 13:00 (theme mode)")

    # ── 섹터 전환용 재선별(--reval): 전환 토글 ON 일 때만, 정본 선별 후 주기 실행 ──
    # 선별 로직은 정본과 동일(leader_finder 무변경). 결과는 <날짜>_reval.json 으로
    # 분리 저장하고 디스코드는 생략한다(전환 확정 알림은 leader_trader 가 발송).
    # 정본과 동일한 KIS 통합 거래대금을 쓴다(공정 비교 위해) → 재선별마다 KIS
    # UN 재조회 ~20콜. 매매봇과 파일락 게이트 공유하므로 interval 을 너무 짧게 두지 말 것.
    _last_reval: dict[str, datetime | None] = {"t": None}

    def _leader_reval_tick():
        if not settings.leader_switch_enabled:
            return
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        try:
            uh, um = (int(x) for x in settings.leader_switch_until.split(":")[:2])
        except Exception:
            uh, um = 13, 0
        t = now.time()
        if t < dtime(9, 40) or (now.hour, now.minute) > (uh, um):
            return
        canonical = _ROOT / "data" / "leader_picks" / f"{now:%Y-%m-%d}.json"
        if not canonical.exists():
            return  # 정본 선별 전엔 재선별 무의미
        iv = max(5, settings.leader_switch_interval_min)
        last = _last_reval["t"]
        if last is not None and (now - last).total_seconds() < iv * 60:
            return
        _last_reval["t"] = now
        cmd = [sys.executable, str(_ROOT / "leader_finder.py"),
               "--once", "--theme", "--summary-only", "--reval", *_selection_args()]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=540, cwd=str(_ROOT),
            )
            # 결과 내용까지 로깅 → '재선별만 뜨고 뭘 했는지 모른다' 방지.
            reval_path = _ROOT / "data" / "leader_picks" / f"{now:%Y-%m-%d}_reval.json"
            head = ""
            try:
                payload = json.loads(reval_path.read_text(encoding="utf-8"))
                leaders = payload.get("leaders", []) or []
                if leaders:
                    parts = [
                        f"{L.get('sector', '?')}/{L.get('name', '?')} "
                        f"{float(L.get('change_pct', 0)):+.1f}% "
                        f"(상승{int(L.get('sector_risers', 0) or 0)})"
                        for L in leaders[:3]
                    ]
                    head = " | 상위: " + " · ".join(parts)
                else:
                    head = " | 선별 없음(핫섹터 미형성)"
            except Exception:
                head = " | (결과 파일 읽기 실패)"
            logger.info(
                "leader reval [{:%H:%M}] 재선별 완료(exit={}){}",
                now, r.returncode, head,
            )
        except subprocess.TimeoutExpired:
            logger.warning("leader reval 타임아웃 (540초)")
        except Exception as e:
            logger.warning("leader reval 실패: {}", e)

    scheduler.add_job(
        _leader_reval_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="*"),
        id="leader_reval",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader reval scheduled: mon-fri 9-13 every minute (gated by switch toggle+interval)")

    # ── 눌림목 매매: 평일 장중 매분 tick ──
    # LEADER_TRADE_ENABLED=off 여도 tick 은 항상 돈다 — 관전 모드(2026-07-16):
    # 분봉 조회·차트 스냅샷·신호 판정은 동일하게 수행하고 주문만 가상으로
    # 로그/알림에 남긴다(실주문·DB기록 없음). 정상 동작 여부 검증용.
    leader_trader = LeaderTrader(broker)

    def _leader_trade_tick():
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

    # ── 마감 후 캐시 스냅샷: 15:35 1회 ──
    # 정본/reval 은 13:00 종료 → 오후 슬롯 및 마감값이 캐시에 안 남음.
    # 마감 직후 rank_df 만 받아 market_flow/intraday_flow 갱신.
    def _leader_cache_snapshot():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        cmd = [sys.executable, str(_ROOT / "leader_finder.py"),
               "--cache-only", *_selection_args()]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120, cwd=str(_ROOT),
            )
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            logger.info("leader cache snapshot [{:%H:%M}] exit={} · {}",
                        now, r.returncode, tail[0])
        except subprocess.TimeoutExpired:
            logger.warning("leader cache snapshot 타임아웃 (120초)")
        except Exception as e:
            logger.warning("leader cache snapshot 실패: {}", e)

    scheduler.add_job(
        _leader_cache_snapshot,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35),
        id="leader_cache_snapshot",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader cache snapshot scheduled: mon-fri 15:35")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("leader runner shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_leader()
