"""모의투자 시즌 — KIS 모의계좌 3개월 초기화 대응.

KIS 모의투자는 신청일로부터 3개월이 지나면 계좌(예수금·보유종목)가 초기화된다.
초기화되면 브로커 기준 "최종 평가금액" 이 사라지므로, 기간(시즌)마다
승률·실현손익·최종평가를 trades.db 의 paper_season 표에 따로 남긴다.

  · 시즌 = PAPER_APPLY_DATE(신청일) ~ 신청일 + 3개월 (끝날 포함, KST)
  · 마지막 거래일 15:40 이후 시즌을 닫는다 → 성적 스냅샷 저장 + 디스코드 알림 + 정지 플래그
  · 정지 플래그(data/coord/paper_halt.json) 가 있으면 세 봇 모두 신규 매수를 하지 않는다.
    .env.overrides 의 *_TRADE_ENABLED 는 파이 update.sh 가 매분 origin 값으로 되돌리므로
    봇 정지는 설정 파일이 아니라 이 플래그 파일로 한다.
  · 재개: 대시보드 "재개" 버튼, 또는 PAPER_APPLY_DATE 를 새 신청일로 바꾸면 그날부터 자동 해제.

check() 는 웹(stock-web) 데몬 스레드가 5분마다 부른다. 봇 쪽은 halted() 만 본다.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

from stock_bot.market_calendar import KST as _KST

_ROOT = Path(__file__).resolve().parents[2]
_DIR = _ROOT / "data" / "coord"
HALT_PATH = _DIR / "paper_halt.json"
STATE_PATH = _DIR / "paper_season.json"

CLOSE_HHMM = (15, 40)       # 마지막 거래일 이 시각 이후 시즌 마감
LASTDAY_ALERT_HHMM = (8, 0)  # 마지막 거래일 아침 알림
D_ALERT = 7                  # 종료 D-7 알림


# ── 기간 계산 ───────────────────────────────────────────────────────────────
def parse_date(s: str | None) -> date | None:
    try:
        return datetime.strptime(str(s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    import calendar
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def season_bounds(apply: date) -> tuple[date, date]:
    """(시작, 끝) — 끝날 포함. 2026-11-17 → 2027-02-17."""
    return apply, add_months(apply, 3)


def last_trading_day(start: date, end: date) -> date:
    """기간 안 마지막 거래일 (끝날부터 거꾸로)."""
    from stock_bot.market_calendar import is_trading_day
    d = end
    while d > start:
        if is_trading_day(datetime(d.year, d.month, d.day)):
            return d
        d -= timedelta(days=1)
    return start


# ── 정지 플래그 ─────────────────────────────────────────────────────────────
_halt_cache: dict = {"mtime": None, "checked": 0.0, "val": False}


def halted() -> bool:
    """정지 플래그 존재 여부. 매수 경로에서 자주 불리므로 2초 캐시."""
    import time
    now = time.monotonic()
    if now - _halt_cache["checked"] < 2.0:
        return _halt_cache["val"]
    _halt_cache["checked"] = now
    try:
        _halt_cache["val"] = HALT_PATH.exists()
    except OSError:
        pass
    return _halt_cache["val"]


def halt_info() -> dict | None:
    try:
        return json.loads(HALT_PATH.read_text(encoding="utf-8") or "{}")
    except FileNotFoundError:
        return None
    except Exception:
        return {"reason": "(읽기 실패)"}


def _write_json(path: Path, data: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass


def set_halt(reason: str, season_start: str) -> None:
    _write_json(HALT_PATH, {
        "reason": reason, "season_start": season_start,
        "ts": datetime.now(_KST).strftime("%Y-%m-%d %H:%M:%S"),
    })
    _halt_cache["checked"] = 0.0


def clear_halt() -> None:
    try:
        HALT_PATH.unlink()
    except FileNotFoundError:
        pass
    _halt_cache["checked"] = 0.0


def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _mark(season_start: str, key: str) -> bool:
    """알림 1회 보장 — 처음 표시하면 True."""
    st = _state()
    s = st.setdefault(season_start, {})
    if s.get(key):
        return False
    s[key] = datetime.now(_KST).strftime("%Y-%m-%d %H:%M")
    _write_json(STATE_PATH, st)
    return True


# ── 시즌 기록 ───────────────────────────────────────────────────────────────
def _stats(start: str | None, end: str | None) -> dict:
    from stock_bot.web.services import _win_stats
    return _win_stats(weeks=0, start_date=start, end_date=end)


def _row_dict(r) -> dict:
    try:
        groups = json.loads(r.by_group or "{}")
    except Exception:
        groups = {}
    return {
        "start_date": r.start_date, "end_date": r.end_date, "kind": r.kind,
        "initial_capital": r.initial_capital, "final_eval": r.final_eval,
        "realized_pnl": r.realized_pnl, "n": r.n, "win": r.win,
        "gross_win": r.gross_win, "gross_loss": r.gross_loss,
        "by_group": groups,
        "closed_at": (r.closed_at + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M") if r.closed_at else None,
        "note": r.note or "",
    }


def _ensure_rows(start: date, end: date) -> None:
    """현재 시즌 행 + (처음 한 번) 첫 시즌 이전 거래를 묶은 '이전 기록' 행."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from stock_bot.config import settings
    from stock_bot.storage.db import ENGINE, PaperSeason, TradeLog

    s0 = start.isoformat()
    with Session(ENGINE) as s:
        rows = {r.start_date: r for r in s.scalars(select(PaperSeason)).all()}
        cur = rows.get(s0)
        if cur is None:
            cur = PaperSeason(start_date=s0, end_date=end.isoformat(), kind="season",
                              initial_capital=float(settings.initial_capital_krw or 0))
            s.add(cur)
        elif cur.closed_at is None:
            cur.end_date = end.isoformat()
            cur.initial_capital = float(settings.initial_capital_krw or 0)

        if not any(r.kind == "season" and r.start_date < s0 for r in rows.values()) \
                and not any(r.kind == "legacy" for r in rows.values()):
            first = s.scalars(select(TradeLog.ts).order_by(TradeLog.ts).limit(1)).first()
            if first is not None:
                lstart = settings.perf_start_date or \
                    (first + timedelta(hours=9)).strftime("%Y-%m-%d")
                lend = (start - timedelta(days=1)).isoformat()
                if lstart <= lend:
                    st = _stats(lstart, lend)
                    o = st["overall"]
                    s.add(PaperSeason(
                        start_date=lstart, end_date=lend, kind="legacy",
                        initial_capital=float(settings.initial_capital_krw or 0),
                        final_eval=None, realized_pnl=o["pnl"], n=o["n"], win=o["win"],
                        gross_win=o["gross_win"], gross_loss=o["gross_loss"],
                        by_group=json.dumps(st["by_group"], ensure_ascii=False),
                        closed_at=datetime.utcnow(),
                        note="시즌 기록 도입 전 (최종평가 없음)"))
        s.commit()


def _close_season(start: date, end: date, now: datetime) -> dict:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from stock_bot.storage.db import ENGINE, PaperSeason

    st = _stats(start.isoformat(), end.isoformat())
    o = st["overall"]
    final_eval, note = None, ""
    if now.date() <= end:
        try:
            from stock_bot.web.services import _account_summary
            acc = _account_summary(force=True) or {}
            if acc.get("available") and float(acc.get("total_eval") or 0) > 0:
                final_eval = float(acc["total_eval"])
            else:
                note = "계좌 조회 실패 — 최종평가 없음"
        except Exception as e:  # noqa: BLE001
            note = f"계좌 조회 오류 — 최종평가 없음 ({type(e).__name__})"
    else:
        note = "기간 종료 후 마감 — 계좌가 이미 초기화됐을 수 있어 최종평가 생략"

    with Session(ENGINE) as s:
        row = s.scalars(select(PaperSeason).where(PaperSeason.start_date == start.isoformat())).first()
        if row is None:
            row = PaperSeason(start_date=start.isoformat(), kind="season")
            s.add(row)
        row.end_date = end.isoformat()
        row.final_eval = final_eval
        row.realized_pnl = o["pnl"]
        row.n, row.win = o["n"], o["win"]
        row.gross_win, row.gross_loss = o["gross_win"], o["gross_loss"]
        row.by_group = json.dumps(st["by_group"], ensure_ascii=False)
        row.closed_at = datetime.utcnow()
        row.note = note
        s.commit()
        return _row_dict(row)


def _fmt_won(v) -> str:
    return "-" if v is None else f"{v:+,.0f}원"


def check(now: datetime | None = None, notify=None) -> str:
    """웹 스레드가 주기적으로 호출. 무엇을 했는지 짧은 문자열로 돌려준다(테스트용)."""
    from stock_bot.config import settings
    if notify is None:
        from stock_bot.notify.discord import notify as notify  # noqa: PLW0127
    now = now or datetime.now(_KST)
    if not settings.is_paper:
        return "실계좌"
    apply = parse_date(getattr(settings, "paper_apply_date", ""))
    if apply is None:
        return "신청일 없음"
    start, end = season_bounds(apply)
    s0 = start.isoformat()
    today = now.date()
    if today < start:
        return "시작 전"

    # 새 신청일로 바뀌었으면 이전 시즌 정지 플래그 자동 해제
    info = halt_info()
    if info and info.get("season_start") and info["season_start"] != s0 and not _is_closed(s0):
        clear_halt()
        notify(f"▶️ 모의투자 새 기간 시작 {s0} ~ {end.isoformat()} — 봇 정지 해제, 매수 재개")
        logger.info("모의 시즌: 새 신청일 {} — 정지 해제", s0)

    _ensure_rows(start, end)
    if _is_closed(s0):
        return "마감됨"

    last = last_trading_day(start, end)
    hm = (now.hour, now.minute)
    if (last - today).days <= D_ALERT and today < last and _mark(s0, "d7"):
        notify(f"⏳ 모의투자 기간 종료 D-{(last - today).days}: 마지막 거래일 {last.isoformat()} "
               f"(기간 {s0} ~ {end.isoformat()}). 마감 후 성적을 저장하고 모든 봇 매수를 정지합니다.")
    if today == last and hm >= LASTDAY_ALERT_HHMM and _mark(s0, "lastday"):
        notify(f"📅 오늘이 모의투자 마지막 거래일 ({last.isoformat()}). "
               f"15:40 이후 성적 저장 + 모든 봇 매수 정지.")
    if today > last or (today == last and hm >= CLOSE_HHMM):
        row = _close_season(start, end, now)
        set_halt(f"모의투자 기간 종료 ({s0} ~ {end.isoformat()})", s0)
        rate = (row["win"] / row["n"] * 100) if row["n"] else None
        pf = (row["gross_win"] / row["gross_loss"]) if row["gross_loss"] else None
        lines = [
            f"🛑 모의투자 기간 종료 — {s0} ~ {end.isoformat()}",
            f"원금 {row['initial_capital']:,.0f}원 · 최종평가 "
            + (f"{row['final_eval']:,.0f}원 ({(row['final_eval'] / row['initial_capital'] - 1) * 100:+.2f}%)"
               if row["final_eval"] and row["initial_capital"] else "없음"),
            f"실현손익 {_fmt_won(row['realized_pnl'])} · 승률 "
            + (f"{rate:.1f}% ({row['win']}/{row['n']})" if rate is not None else "-")
            + (f" · 손익비 {pf:.2f}" if pf else ""),
        ]
        for g, a in (row["by_group"] or {}).items():
            if a.get("n"):
                lines.append(f"  · {g}: {a['win']}/{a['n']} · {_fmt_won(a.get('pnl'))}")
        if row["note"]:
            lines.append(f"※ {row['note']}")
        lines.append("모든 봇 신규 매수 정지. 모의투자 재신청 후 파라미터 PAPER_APPLY_DATE 를 "
                     "새 신청일로 바꾸면 그날부터 자동 재개됩니다.")
        notify("\n".join(lines))
        logger.info("모의 시즌 마감: {}", row)
        return "마감"
    return "진행중"


def _is_closed(s0: str) -> bool:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from stock_bot.storage.db import ENGINE, PaperSeason
    with Session(ENGINE) as s:
        r = s.scalars(select(PaperSeason).where(PaperSeason.start_date == s0)).first()
        return bool(r and r.closed_at)


def summary() -> dict:
    """대시보드용 — 저장된 시즌 + 진행 중 시즌 실시간 + 누적."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from stock_bot.config import settings
    from stock_bot.storage.db import ENGINE, PaperSeason

    apply = parse_date(getattr(settings, "paper_apply_date", ""))
    cur_start = apply.isoformat() if apply else None
    with Session(ENGINE) as s:
        rows = [_row_dict(r) for r in s.scalars(select(PaperSeason).order_by(PaperSeason.start_date)).all()]

    out = []
    for r in rows:
        if r["closed_at"] is None:
            if r["start_date"] != cur_start:
                continue   # 신청일이 바뀌어 버려진 미마감 행
            st = _stats(r["start_date"], r["end_date"])
            o = st["overall"]
            r.update(realized_pnl=o["pnl"], n=o["n"], win=o["win"],
                     gross_win=o["gross_win"], gross_loss=o["gross_loss"],
                     by_group=st["by_group"], live=True)
            try:
                from stock_bot.web.services import _account_summary
                acc = _account_summary(cache_only=True) or {}
                if acc.get("available"):
                    r["final_eval"] = float(acc.get("total_eval") or 0) or None
            except Exception:
                pass
        out.append(r)

    def _comb(items: list[dict]) -> dict:
        n = sum(x["n"] or 0 for x in items)
        win = sum(x["win"] or 0 for x in items)
        gw = sum(x["gross_win"] or 0 for x in items)
        gl = sum(x["gross_loss"] or 0 for x in items)
        return {"n": n, "win": win, "rate": (win / n * 100) if n else None,
                "pnl": sum(x.get("realized_pnl", x.get("pnl")) or 0 for x in items),
                "pf": (gw / gl) if gl > 0 else None, "gross_win": gw, "gross_loss": gl}

    total = _comb(out)
    groups: dict = {}
    for g in ("단타", "대장주", "스윙"):
        groups[g] = _comb([dict((x["by_group"] or {}).get(g) or {"n": 0, "win": 0, "pnl": 0,
                                                                 "gross_win": 0, "gross_loss": 0})
                           for x in out])
    total["by_group"] = groups
    for r in out:
        r["rate"] = (r["win"] / r["n"] * 100) if r["n"] else None
        r["pf"] = (r["gross_win"] / r["gross_loss"]) if r["gross_loss"] else None
        if r["final_eval"] and r["initial_capital"]:
            r["return_pct"] = (r["final_eval"] / r["initial_capital"] - 1) * 100
        else:
            r["return_pct"] = None

    period = None
    if apply:
        a, b = season_bounds(apply)
        period = {"start": a.isoformat(), "end": b.isoformat()}
    return {"seasons": out, "total": total, "period": period,
            "halt": halt_info(), "is_paper": settings.is_paper}
