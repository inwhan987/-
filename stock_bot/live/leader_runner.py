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
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
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
        "--min-value-anchor-hhmm", str(settings.leader_sel_min_value_anchor_hhmm),
        "--max-value", str(float(settings.leader_sel_max_value_eok)),
        "--min-value-floor", str(float(settings.leader_sel_min_value_floor_eok)),
        "--pick-window-end", str(settings.leader_switch_until),
        "--dyn-value-pct", str(float(settings.leader_sel_dyn_value_pct)),
        "--mf-clamp-low",    str(float(settings.leader_mf_clamp_low)),
        "--mf-clamp-high",   str(float(settings.leader_mf_clamp_high)),
        "--min-mktcap", str(float(settings.leader_sel_min_cap_eok)),
        "--max-change", str(float(settings.leader_sel_max_change)),
        "--turnover-cap-pct",    str(float(settings.leader_sel_turnover_cap_pct)),
    ]


def _prefetch_notify(label: str, ok: bool, detail: str) -> None:
    """캐시 프리페치 결과를 디스코드로 통지 (2026-08-19).

    프리페치는 새벽·장전에 조용히 도는 작업이라 실패해도 로그를 열어보기 전엔
    알 수 없었다. 선별 품질이 이 캐시들에 직결되므로 완료/실패를 모두 알린다.
    빈도가 낮아(하루 2~3회) 알림 소음 부담은 없다.
    """
    head = "✅" if ok else "⚠️"
    try:
        notify(f"👑 **대장주 캐시** {head} {label}\n{detail}")
    except Exception as e:  # 알림 실패가 프리페치 결과를 삼키면 안 됨
        logger.warning("prefetch notify 실패({}): {}", label, e)


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
    # 캐시 갱신. 09:30 첫 pick tick 전에 완료되어야 배수 계산이 유효해진다.
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
                timeout=600, cwd=str(_ROOT),
            )
            # 요약 + 날짜별 kospi/kosdaq 다 표시
            body = (r.stdout or r.stderr or "").strip()
            # leader_finder 출력의 "[prefetch_market_flow] " 프리픽스 이후만 취함
            lines = [ln for ln in body.splitlines()
                     if "[prefetch_market_flow]" in ln or ln.lstrip().startswith("·")]
            detail = "\n  ".join(lines) if lines else body
            logger.info(
                "leader market_flow prefetch [{:%H:%M}] (exit={})\n  {}",
                now, r.returncode, detail,
            )
        except subprocess.TimeoutExpired:
            logger.warning("leader market_flow prefetch 타임아웃 (600초)")
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

    # ── avg_value_nd 프리페치: 매 영업일 02:00, 매경 시총 캐시에서 시총≥1000억
    # 종목 전부(상한 없음, 다움 교집합 없음)를 KIS KRX 순차 호출로 디스크 캐시에 채운다.
    # 새벽엔 09:30까지 7시간 이상 여유가 있어 상한을 두지 않는다(2026-08-12).
    # 09:30 첫 pick tick 이 avg_value_nd() 캐시 히트로 즉시 반환되어 선별
    # 타임아웃(540초) 여유를 크게 확보한다.
    # (다움 top-600 교집합 방식은 그날 거래대금 순위가 낮은 종목이 누락되는
    #  문제로 2026-08-15 폐기 — 시총 조건만으로 전수 프리페치)
    def _leader_prefetch_avgval(sectors_only: bool = False, label: str = "02:00 정기"):
        """거래대금 5일평균 + 업종 캐시 프리페치.

        sectors_only=True 면 업종 캐시만 채운다 — avgval 은 당일 날짜 키라
        비영업일에 채워봐야 다음 영업일 첫 조회에서 통째로 버려지기 때문.
        """
        now = datetime.now(tz=_KST)
        # 오늘이 영업일이 아니면 (공휴일) 스킵 — 어차피 그날 pick 안 함
        # (업종 캐시는 영구값이라 sectors_only 경로는 휴일에도 의미가 있다)
        if not sectors_only and not _is_trading_day(now):
            return
        cmd = [sys.executable, str(_ROOT / "leader_finder.py")]
        cmd += (["--prefetch-sectors"] if sectors_only else
                ["--prefetch-avgval", "--prefetch-fetch-n", "600"])
        cmd += ["--prefetch-min-cap-eok", str(float(settings.leader_sel_min_cap_eok))]
        if not sectors_only:
            cmd += ["--prefetch-pace-sec", "1.0"]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=5400, cwd=str(_ROOT),
            )
            lines = (r.stdout or r.stderr or "").strip().splitlines()
            # 요약 라인만 (진행 % 라인 제외) — 유니버스/대상/캐시hit/완료 + 업종
            summary = [ln for ln in lines
                       if ("[prefetch_avgval]" in ln or "[prefetch_sectors]" in ln)
                       and " 진행 " not in ln]
            logger.info(
                "leader avgval prefetch [{:%H:%M}] (exit={})\n  {}",
                now, r.returncode, "\n  ".join(summary) if summary else "(no output)",
            )
            # 시총 캐시는 avgval 안에서 부수적으로 갱신된다 — 별도 알림으로 분리
            mkcap = [ln.strip() for ln in lines
                     if "[시총 캐시" in ln or "시총 유니버스" in ln]
            if mkcap:
                _prefetch_notify(
                    f"시총 캐시 ({label})",
                    any("신규 크롤링" in ln for ln in mkcap),
                    "\n".join(mkcap),
                )
            done = [ln for ln in summary if " 완료:" in ln or "— 종료" in ln]
            _prefetch_notify(
                ("업종" if sectors_only else "거래대금5일+업종") + f" ({label})",
                r.returncode == 0 and bool(done),
                "\n".join(done) if done else f"exit={r.returncode} · 요약 라인 없음",
            )
        except subprocess.TimeoutExpired:
            logger.warning("leader avgval prefetch 타임아웃 (5400초)")
            _prefetch_notify(f"거래대금5일+업종 ({label})", False, "타임아웃 (5400초)")
        except Exception as e:
            logger.warning("leader avgval prefetch 실패: {}", e)
            _prefetch_notify(f"거래대금5일+업종 ({label})", False, f"실패: {e}")

    scheduler.add_job(
        _leader_prefetch_avgval,
        CronTrigger(day_of_week="mon-fri", hour=2, minute=0),
        id="leader_prefetch_avgval",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader avgval prefetch scheduled: mon-fri 02:00 (시총≥1000억 전수(매경캐시, 다움교집합없음) → KIS KRX 순차 + 업종 캐시 이어서)")

    # ── 테마 구성종목 프리페치: 매 영업일 09:05 ─────────────────────────────
    # 선별 소요의 최대 병목은 네이버 테마 상세 263페이지 재크롤(약 80~120초)이었다.
    # leader_finder 는 tick 마다 새 서브프로세스라 모듈 전역 캐시가 매번 비어,
    # 09:28:30 에 시작해도 종료가 09:31 을 넘고 미선별 재시도마다 같은 크롤을 반복했다.
    # → 09:05 에 한 번 긁어 날짜 키 디스크 캐시(leader_theme_cache.json)에 적재.
    # 08:30 이 아니라 개장 후인 이유: 테마 편입/제외가 개장 무렵 반영될 수 있어
    # 장전 스냅샷은 그날 구성과 어긋날 수 있다. 09:30 까지 25분 여유.
    def _leader_prefetch_themes():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        cmd = [sys.executable, str(_ROOT / "leader_finder.py"), "--prefetch-themes"]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=600, cwd=str(_ROOT),
            )
            lines = [ln for ln in (r.stdout or r.stderr or "").strip().splitlines()
                     if "[prefetch_themes]" in ln]
            logger.info(
                "leader theme prefetch [{:%H:%M}] (exit={}) {}",
                now, r.returncode, lines[-1] if lines else "(no output)",
            )
            done = [ln for ln in lines if " 완료:" in ln]
            _prefetch_notify("테마 구성종목 (09:05)",
                             r.returncode == 0 and bool(done),
                             done[-1] if done else f"exit={r.returncode} · 요약 라인 없음")
        except subprocess.TimeoutExpired:
            logger.warning("leader theme prefetch 타임아웃 (600초) — 선별이 직접 크롤(느림)")
            _prefetch_notify("테마 구성종목 (09:05)", False,
                             "타임아웃 (600초) — 선별이 직접 크롤(느림)")
        except Exception as e:
            logger.warning("leader theme prefetch 실패: {}", e)
            _prefetch_notify("테마 구성종목 (09:05)", False, f"실패: {e}")

    scheduler.add_job(
        _leader_prefetch_themes,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=5),
        id="leader_prefetch_themes",
        max_instances=1,
        coalesce=True,
        # 기본 grace 1초 → 09:05 직전 재기동 시 부팅 백필이 메인스레드를 잡고
        # 있으면 scheduler.start() 가 09:05:01 로 밀려 통째로 미스파이어된다
        # (실측 2026-08-20: 09:03 배포 → 테마 프리페치 스킵 → 픽이 263테마
        #  직접 크롤 47.6초). 픽(09:30)까지는 어차피 여유가 있으니 넉넉히.
        misfire_grace_time=1800,
    )
    logger.info("leader theme prefetch scheduled: mon-fri 09:05 (네이버 테마 263개 구성종목 → 디스크 캐시)")

    # ── 부팅 직후 캐시 백필 1회 (2026-08-19) ─────────────────────────────
    # 재시작·초기화(볼륨 리셋, 캐시 파일 삭제) 후에는 avgval/업종 디스크 캐시가
    # 비어 있어 선별이 콜드 경로(210초)를 타고, 02:00 크론은 다음 날에나 온다.
    # → 기동 시 캐시 상태를 보고 비어 있으면 그 자리에서 한 번 다 채운다.
    #
    # 장중(_is_market_open)이면 실행하지 않는다: avgval 전수는 KIS 1건/초 ×
    # 1200종목 ≈ 20분, 업종은 네이버 크롤 ≈ 6분이라 장중에 돌리면 매매 tick 과
    # KIS 유량을 다투고 선별 tick 과도 겹친다. 장중에 캐시가 비어 있으면 선별이
    # 스스로 조회하고 결과를 캐시에 적재하므로(run_once → _save_*_cache) 자가치유된다.
    #
    # 테마 캐시는 여기서 채우지 않는다 — 개장 무렵 편입/제외를 반영해야 해서
    # 09:05 크론이 개장 후에 긁는 설계이고, 장전에 미리 채워두면 09:05 프리페치가
    # 캐시 히트로 no-op 이 되어 장전 스냅샷이 그날 하루 굳어버린다.
    def _leader_boot_cache_backfill():
        now = datetime.now(tz=_KST)
        if _is_market_open(now):
            logger.info("leader 캐시 부팅 백필 스킵: 장중")
            return
        today = f"{now:%Y%m%d}"
        n_avg = n_sec = 0
        try:
            raw = json.loads((_ROOT / "data" / "leader_avgval_cache.json")
                             .read_text(encoding="utf-8"))
            n_avg = sum(1 for v in raw.values()
                        if isinstance(v, dict) and v.get("date") == today
                        and float(v.get("avg") or 0) > 0)
        except Exception:
            n_avg = 0
        try:
            raw = json.loads((_ROOT / "data" / "leader_sector_cache.json")
                             .read_text(encoding="utf-8"))
            n_sec = len(raw.get("codes") or {})
        except Exception:
            n_sec = 0
        need_avg = _is_trading_day(now) and n_avg == 0
        need_sec = n_sec == 0
        if not (need_avg or need_sec):
            logger.info("leader 캐시 부팅 백필 스킵: avgval(오늘자) {}건 · 업종 {}건",
                        n_avg, n_sec)
            return
        logger.info("leader 캐시 부팅 백필 시작: avgval(오늘자) {}건 · 업종 {}건 "
                    "→ {} 실행", n_avg, n_sec,
                    "avgval+업종" if need_avg else "업종만")
        # need_avg 면 avgval 전수 → 그 끝에서 업종 프리페치가 이어서 돈다.
        _leader_prefetch_avgval(sectors_only=not need_avg, label="부팅 백필")

    # 기동 직후 스케줄러 스레드풀에서 실행 — run_leader() 를 20~30분 블로킹하면
    # 매매 tick·선별 크론 등록이 그만큼 늦어지므로 인라인 호출하지 않는다.
    scheduler.add_job(
        _leader_boot_cache_backfill,
        DateTrigger(run_date=datetime.now(tz=_KST) + timedelta(seconds=20)),
        id="leader_boot_cache_backfill",
        max_instances=1,
        coalesce=True,
        # 기본 misfire_grace_time=1초 — 등록~start() 사이가 20초를 넘으면
        # 백필이 통째로 스킵된다. 부팅 1회짜리라 유예를 넉넉히 준다.
        misfire_grace_time=3600,
    )
    logger.info("leader 캐시 부팅 백필 예약: 기동 +20초 (장중이면 자동 스킵)")

    # ── 부팅 직후 백필 1회 (무조건).
    # 낮에 라이브 run_once 가 max 로 기록한 부분값(13:00 부근) 을 pykrx close 로
    # 덮어써 최근 5영업일 정확화. 08:30 크론 기다리지 않고 배포/재기동 즉시 갱신.
    # (2026-08-11: 스키마·일수 조건 제거 — 매 부팅마다 5일창 강제 재확정.)
    try:
        logger.info("leader market_flow 부팅 직후 백필 1회 실행 (최근 5영업일 강제 덮어쓰기)")
        _leader_prefetch_market_flow()
    except Exception as e:
        logger.warning("leader market_flow 부팅 백필 실패: {}", e)

    # ── 대장주 선별 (테마 모드): 9:30 첫 시도 → 미선별 시 10분 간격, 13:00 마지막 ──
    # 선별 성공(data/leader_picks/날짜.json 생성) 시 그날은 중지.
    # 디스코드 알림은 leader_finder.py 가 매 시도마다 직접 발송('없음' 포함).
    def _pick_log_block(tail: list[str], limit: int = 80) -> str:
        """선별 subprocess stdout 을 로그 한 덩어리로 정리 (2026-08-19).

        예전엔 성공 시 tail[-1] 한 줄, 미선별 시 "조건 충족 대장주 없음" 이후
        30줄만 남겼다. 둘 다 위치 기반이라 출력이 한 줄만 늘어도 남는 내용이
        바뀐다 — 실제로 _summary_text 가 수십 줄짜리 블록이라 성공 로그엔
        바스켓 끝자락 한 줄만 찍히고 있었고, [단계별 소요] 를 덧붙이자 그 한
        줄마저 계측으로 바뀌었다. --summary-only 는 출력이 30~50줄이라 전량을
        남겨도 부담이 없고, 잘려 나가던 [시총 캐시]·[시간비례]·계측이 전부
        진단에 쓰는 값이다. 그래서 위치로 자르지 않고 전량 + 상한만 둔다.
        """
        lines = [ln.rstrip() for ln in tail]
        if len(lines) <= limit:
            return "\n".join(lines)
        # 상한 초과 시에도 머리(환경·캐시 상태)와 꼬리(요약·계측)는 보존한다.
        head, foot = lines[:10], lines[-(limit - 11):]
        return "\n".join(head + [f"  … 중략 {len(lines) - len(head) - len(foot)}줄 …"] + foot)

    def _leader_pick_tick():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now):
            return
        t = now.time()
        # 2026-08-19: 09:30:00 정시로 환원. 예전 9:28:30 은 선별이 ~210초 걸려서
        # 9:30 에 picks 가 완성되도록 앞당긴 보정이었는데, 테마·업종 디스크 캐시
        # 도입으로 실측 7.8초가 되어 보정이 불필요해졌다(계측: 210.6→7.8초).
        if t < dtime(9, 30) or t > dtime(13, 0):
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
                    "leader pick [{:%H:%M}] 선별 완료 — 오늘 스케줄 종료 (exit={})\n{}",
                    now, r.returncode, _pick_log_block(tail),
                )
            else:
                # 미선별: 성공과 같은 규칙으로 stdout 전량(상한 내) 을 남긴다.
                # 예전엔 "조건 충족 대장주 없음" 마커 이후만 잡아 그 앞의 거래대금
                # 하한 산출·시총 캐시 상태가 빠졌는데, 미선별 원인 판정엔 오히려
                # 그쪽이 필요했다.
                logger.info(
                    "leader pick [{:%H:%M}] 미선별 — 10분 후 재시도 (exit={})\n{}",
                    now, r.returncode, _pick_log_block(tail),
                )
        except subprocess.TimeoutExpired:
            logger.warning("leader pick 타임아웃 (540초) — 다음 회차에 재시도")
        except Exception as e:
            logger.warning("leader pick 실패: {}", e)

    scheduler.add_job(
        _leader_pick_tick,
        # minute="0-50/10" = 0,10,20,30,40,50 분 정시 → 9:30, 9:40, ... 13:00.
        # 9:00·9:10·9:20 발화는 위 게이트(< 9:30)가 막아 첫 실행은 9:30:00.
        CronTrigger(day_of_week="mon-fri", hour="9-13", minute="0-50/10", second=0),
        id="leader_pick",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader pick scheduled: mon-fri 9:30:00 → 10min retry until 13:00 (theme mode)")

    # ── 섹터 전환용 재선별(--reval): 전환 토글 ON 일 때만, 정본 선별 후 주기 실행 ──
    # 선별 로직은 정본과 동일(leader_finder 무변경). 결과는 <날짜>_reval.json 으로
    # 분리 저장하고 디스코드는 생략한다(전환 확정 알림은 leader_trader 가 발송).
    # 정본과 동일한 KIS 통합 거래대금을 쓴다(공정 비교 위해) → 재선별마다 KIS
    # UN 재조회 ~20콜. 매매봇과 파일락 게이트 공유하므로 interval 을 너무 짧게 두지 말 것.
    _last_reval: dict[str, datetime | None] = {"t": None}
    # 직전 재선별 스냅샷(날짜, [(섹터, 점수100, [(종목명, rank), ...]), ...]) —
    # 로그에 "어디서 어디로 바뀌었는지"를 찍기 위한 비교 기준.
    _reval_snap: dict[str, Any] = {"date": "", "snap": None}

    def _reval_shape(leaders: list) -> list:
        """재선별 결과를 비교용 최소 형태로 축약."""
        return [
            (L.get("sector", "?"),
             float(L.get("sector_score_100", 0) or 0),
             [(m.get("name", "?"), int(m.get("rank", j + 1) or j + 1))
              for j, m in enumerate(
                  sorted((L.get("top3") or []), key=lambda x: x.get("rank", 9))[:3])])
            for L in leaders[:3]
        ]

    def _reval_diff(cur: list, prev: list | None) -> str:
        """이전 스냅샷 대비 섹터 순위·섹터내 종목 순위 변동을 사람이 읽는 문장으로."""
        cur_names = {c[0] for c in cur}
        prev_rank = {sec: i for i, (sec, _, _) in enumerate(prev or [])}
        prev_stk = {sec: dict(st) for sec, _, st in (prev or [])}

        sec_parts = []
        for i, (sec, sc, _st) in enumerate(cur):
            if prev is None:
                tag = ""
            elif sec not in prev_rank:
                tag = " 신규"
            else:
                d = prev_rank[sec] - i
                tag = f" ↑{d}" if d > 0 else (f" ↓{-d}" if d < 0 else " -")
            sec_parts.append(f"{i + 1}위 {sec}({sc:.1f}{tag})")
        lines = ["섹터: " + " · ".join(sec_parts)]
        gone = [sec for sec in prev_rank if sec not in cur_names]
        if gone:
            lines.append("섹터 이탈: " + ", ".join(gone))

        for sec, _sc, st in cur:
            pv = prev_stk.get(sec)
            parts = []
            for name, rk in st:
                if pv is None or name not in pv:
                    parts.append(f"{rk}등 {name}" + ("" if pv is None else "(신규)"))
                elif pv[name] != rk:
                    parts.append(f"{rk}등 {name}({pv[name]}등→{rk}등)")
                else:
                    parts.append(f"{rk}등 {name}")
            line = f"  {sec}: " + " · ".join(parts)
            dropped = [n for n in (pv or {}) if n not in {x[0] for x in st}]
            if dropped:
                line += " | 빠짐: " + ", ".join(dropped)
            lines.append(line)
        return (chr(10) + "    ").join(lines)

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
        # 슬롯을 다 쓰면 전환 자체가 무의미(leader_trader 는 slots_open 일 때만
        # _maybe_switch 호출) — KIS 재조회·서브프로세스 낭비 방지.
        # 판정 기준을 leader_trader 와 동일하게 "빈 슬롯이 있느냐"로 맞춘다.
        # status 문자열로 게이트하면 leader_max_positions>1 일 때 어긋난다 —
        # 1건 보유 + 슬롯 여유면 trader 는 전환을 계속 하려 하는데 status 는
        # "holding" 이라 재선별이 멈춰, 낡은 reval.json 만 보게 된다.
        try:
            state_path = _ROOT / "data" / "leader_trade_state" / f"{now:%Y-%m-%d}.json"
            positions = json.loads(state_path.read_text(encoding="utf-8")).get("positions") or {}
            if len(positions) >= max(1, settings.leader_max_positions):
                return
        except Exception:
            pass
        iv = max(5, settings.leader_switch_interval_min)
        last = _last_reval["t"]
        if last is None:
            # 기동 후 첫 재선별 — 정본 선별 직후에 곧바로 도는 걸 막는다.
            # 정본 저장 시각을 '마지막 재선별'로 간주해 interval 만큼 대기
            # (실측 2026-08-20: 09:40 선별 완료 09:41 → 09:42 재선별 → 09:43
            #  섹터 재정렬. 90초 전 결과를 KIS 20콜 + 서브프로세스 28초를 더
            #  써서 다시 뽑은 셈).
            try:
                last = datetime.fromtimestamp(canonical.stat().st_mtime, tz=_KST)
            except Exception:
                last = None
        # 관용 10초: 크론은 매분 :00 에 발화하지만 now 는 job 진입 시각(ms 지터)이라
        # 경계가 iv*60 에 딱 걸리면 elapsed 가 299.99초로 계산돼 한 번 건너뛰고
        # 다음 분에 실행된다 → 5분 주기가 6분으로 드리프트. (2026-08-19 수정)
        if last is not None and (now - last).total_seconds() < iv * 60 - 10:
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
            today = f"{now:%Y-%m-%d}"
            head = ""
            try:
                payload = json.loads(reval_path.read_text(encoding="utf-8"))
                leaders = payload.get("leaders", []) or []
                if leaders:
                    cur = _reval_shape(leaders)
                    prev = _reval_snap["snap"] if _reval_snap["date"] == today else None
                    head = (chr(10) + "    ") + _reval_diff(cur, prev)
                    _reval_snap["date"], _reval_snap["snap"] = today, cur
                else:
                    head = " | 선별 없음(핫섹터 미형성)"
                    _reval_snap["date"], _reval_snap["snap"] = today, []
            except Exception:
                head = " | (결과 파일 읽기 실패)"
            logger.info(
                "leader reval [{:%H:%M}] 순위계산 완료(exit={}){}"
                " — 전환/추가는 leader_trader 🔄 섹터 재정렬 로그 참고",
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

    # ── 보유 중 손절/익절 초단위 체크(2026-08-15, 5초로 단축) ──
    # 정식 tick(1분)은 entry 스캔(3분봉 확정)까지 겸하느라 주기를 못 줄이지만,
    # 청산 판단(price vs stop/tp)은 실시간 시세만 있으면 되므로 5초 주기로
    # 별도 실행 — 최대 청산 지연이 1분→5초로 줄어든다. leader_trader.py 의
    # threading.Lock 이 정식 tick 과의 동시 실행을 막고, get_quote(priority=True)
    # 로 KIS 유량 게이트에서 다른 일반 호출보다 먼저 통과한다.
    def _leader_exit_fast_tick():
        now = datetime.now(tz=_KST)
        if not _is_trading_day(now) or not _is_market_open(now):
            return
        try:
            leader_trader.check_exit_fast()
        except Exception as e:
            logger.exception("leader_trader check_exit_fast 실패: {}", e)

    scheduler.add_job(
        _leader_exit_fast_tick,
        CronTrigger(day_of_week="mon-fri", hour="9-15", second="*/5"),
        id="leader_exit_fast",
        max_instances=1,
        coalesce=True,
    )
    logger.info("leader exit-fast scheduled: mon-fri 9-15 every 5s (holding 시 stop/tp 체크, priority gate)")

    # 15:35 마감 캐시 스냅샷 제거(2026-08-11) — 다음날 08:30 pykrx 백필이
    # 어제(=오늘) 값을 정확히 다시 채워 완전 중복. baseline 은 오직 pykrx
    # 소스로만 통일해 스케일 일관성↑.

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("leader runner shutting down")
    finally:
        broker.close()


if __name__ == "__main__":
    run_leader()
