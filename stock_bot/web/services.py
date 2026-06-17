"""웹 대시보드 데이터 계층 — DB 조회·브로커 상태·대장주 표시용 헬퍼.

app.py(create_app 라우트)에서 쓰는 순수 데이터 접근/조회 함수를 모았다.
동작 불변(behavior-preserving) 추출 — 라우트 로직은 건드리지 않고 이 모듈을
import 해서 그대로 사용한다. 브로커 API 실패해도 페이지가 떠야 하므로 모든
외부 호출은 try/except 로 감싼다.

상태(state) 주의:
  · _POSITIONS_CACHE / _ACCOUNT_CACHE 는 in-place 로 변형(mutate)되는 dict 라
    app.py 가 import 한 동일 객체를 공유한다.
  · _broker_instance 싱글턴은 이 모듈 안에서만 재할당된다(global).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from stock_bot.config import settings
from stock_bot.names import get_name
from stock_bot.market_calendar import KST as _KST
from stock_bot.news.store import NEWS_ENGINE, NewsRow
from stock_bot.storage.db import ENGINE as TRADE_ENGINE
from stock_bot.storage.db import ReviewLog, TradeLog


def _kst(dt: datetime) -> str:
    """UTC naive datetime → KST 문자열 (DB 저장값이 UTC 기준이므로 +9h)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S")


def _recent_trades(limit: int = 30) -> list[dict]:
    import json as _json
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(TradeLog).order_by(desc(TradeLog.ts)).limit(limit * 3)).all()
        out: list[dict] = []
        for r in rows:
            try:
                broker_resp = _json.loads(r.broker_response) if r.broker_response else {}
            except Exception:
                broker_resp = {}
            if broker_resp.get("dry_run"):
                continue
            details = {}
            raw = getattr(r, "details", "") or ""
            if raw:
                try:
                    details = _json.loads(raw)
                except Exception:
                    details = {"raw": raw}
            avg_price = details.get("avg_price", 0.0) or 0.0
            pnl_pct = (
                (r.price - avg_price) / avg_price * 100
                if r.side == "sell" and avg_price > 0
                else None
            )
            out.append(
                {
                    "id": r.id,
                    "ts": _kst(r.ts),
                    "symbol": r.symbol,
                    "name": get_name(r.symbol),
                    "side": r.side,
                    "quantity": r.quantity,
                    "price": r.price,
                    "avg_price": avg_price,
                    "pnl_pct": pnl_pct,
                    "reason": r.reason,
                    "strategy": getattr(r, "strategy", "") or "",
                    "details": details,
                }
            )
        return out


def _symbol_trades(code: str, date: str = "") -> list[dict]:
    """차트 마커용 — 특정 종목의 (선택 날짜) 실거래만 시각 오름차순 반환.

    code: 6자리 bare 코드(접미사 무시). date: "YYYYMMDD" (없으면 오늘 KST).
    반환: [{ts, hhmmss, side, price, quantity, strategy, reason, pnl_pct}] (dry_run 제외).
    """
    import json as _json

    bare = str(code).split(".")[0]
    if not date:
        date = datetime.now(_KST).strftime("%Y%m%d")
    out: list[dict] = []
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(TradeLog).order_by(TradeLog.ts)).all()
        for r in rows:
            if str(r.symbol).split(".")[0] != bare:
                continue
            kst = _kst(r.ts)                       # "YYYY-MM-DD HH:MM:SS"
            ymd = kst[:10].replace("-", "")
            if ymd != date:
                continue
            try:
                broker_resp = _json.loads(r.broker_response) if r.broker_response else {}
            except Exception:
                broker_resp = {}
            if broker_resp.get("dry_run"):
                continue
            details = {}
            raw = getattr(r, "details", "") or ""
            if raw:
                try:
                    details = _json.loads(raw)
                except Exception:
                    details = {}
            avg_price = details.get("avg_price", 0.0) or 0.0
            pnl_pct = (
                (r.price - avg_price) / avg_price * 100
                if r.side == "sell" and avg_price > 0 else None
            )
            out.append({
                "ts": kst,
                "hhmmss": kst[11:].replace(":", ""),   # "HHMMSS" — 차트 봉 매칭용
                "side": r.side,
                "price": r.price,
                "quantity": r.quantity,
                "strategy": getattr(r, "strategy", "") or "",
                "reason": r.reason or "",
                "pnl_pct": pnl_pct,
            })
    return out


def _recent_reviews(limit: int = 30) -> list[dict]:
    import json as _json
    with Session(TRADE_ENGINE) as s:
        rows = s.scalars(select(ReviewLog).order_by(desc(ReviewLog.ts)).limit(limit)).all()
        out = []
        for r in rows:
            def _dec(x):
                try:
                    return _json.loads(x or "[]")
                except Exception:
                    return []
            out.append({
                "id": r.id,
                "ts": _kst(r.ts),
                "date": r.date,
                "trades_count": r.trades_count,
                "summary": r.summary,
                "findings": _dec(r.findings),
                "suggestions": _dec(r.suggestions),
            })
        return out


def _recent_news(limit: int = 10) -> list[dict]:
    import re as _re

    def _title_key(t: str) -> str:
        """대괄호 태그·공백 제거 후 앞 30자로 중복 판정."""
        t = _re.sub(r"^\[[^\]]+\]\s*", "", t or "")
        t = _re.sub(r"[\s·ㆍ]+", "", t)
        return t[:30]

    with Session(NEWS_ENGINE) as s:
        rows = s.scalars(
            select(NewsRow).order_by(desc(NewsRow.published_at)).limit(limit * 4)
        ).all()
        seen: set[str] = set()
        result: list[dict] = []
        for r in rows:
            if not r.title or not r.title.strip():
                continue
            key = _title_key(r.title)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "symbol": r.symbol,
                "name": get_name(r.symbol),
                "title": r.title,
                "url": r.url,
                "publisher": r.publisher,
                "published_at": r.published_at.strftime("%Y-%m-%d %H:%M"),
                "score": r.sentiment_score,
                "method": r.sentiment_method,
                "is_critical": bool(getattr(r, "is_critical", False)),
                "weight": float(getattr(r, "weight", 1.0)),
            })
            if len(result) >= limit:
                break
        return result


def _news_window_label() -> dict:
    """현재 시점 기준 뉴스 감성 창 정보를 반환.

    Returns:
        {
          "day": "화요일",
          "since_str": "전날 15:30",
          "label": "화요일 · 전날 15:30 ~ 현재",
        }
    """
    from stock_bot.news.store import news_since_kst

    now = datetime.now(tz=_KST)
    wd = now.weekday()  # 0=월
    day_names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    day = day_names[wd]

    if now.hour >= 10:
        since_str = "당일 09:00"
    elif wd == 0:
        since_str = "금요일 15:30"
    else:
        since_str = "전날 15:30"

    since_utc = news_since_kst()
    since_kst = since_utc.replace(tzinfo=timezone.utc).astimezone(_KST)
    since_fmt = since_kst.strftime("%m/%d %H:%M")

    return {
        "day": day,
        "since_str": since_str,
        "since_fmt": since_fmt,
        "label": f"{day} · {since_str} ~ 현재",
    }


def _sentiment_summary() -> tuple[list[dict], dict]:
    from stock_bot.news.store import news_since_kst
    since = news_since_kst()
    window = _news_window_label()
    out: list[dict] = []
    with Session(NEWS_ENGINE) as s:
        for sym in settings.symbols:
            code = sym.split(".")[0]  # 005930.KS → 005930
            rows = s.scalars(
                select(NewsRow).where(NewsRow.symbol == code).where(NewsRow.published_at >= since)
            ).all()
            name = get_name(sym)
            if rows:
                total_w = sum(max(getattr(r, "weight", 1.0), 0.01) for r in rows)
                avg = (
                    sum(r.sentiment_score * max(getattr(r, "weight", 1.0), 0.01) for r in rows)
                    / total_w
                )
                crit = sum(1 for r in rows if getattr(r, "is_critical", False))
                out.append(
                    {"symbol": code, "name": name, "score": avg, "count": len(rows), "critical": crit}
                )
            else:
                out.append({"symbol": code, "name": name, "score": 0.0, "count": 0, "critical": 0})
    return out, window


def _realized_pnl_summary(strategy: str | None = None) -> dict:
    """TradeLog 전체에서 실현손익·거래횟수 계산 (FIFO 매칭).

    strategy 지정 시 해당 전략 거래만 집계 (예: 'leader_pullback' → 대장주만).
    대장주는 settings.symbols 와 종목이 겹치지 않게 설계되어 있어
    전체 = 스톡봇 + 대장주 가 정확히 성립한다.
    """
    from collections import deque
    from datetime import datetime as _dt
    import json as _json2
    def _is_dry(r) -> bool:
        try:
            return bool(_json2.loads(r.broker_response or "{}").get("dry_run"))
        except Exception:
            return False
    with Session(TRADE_ENGINE) as s:
        rows = [r for r in s.scalars(select(TradeLog).order_by(TradeLog.ts)).all() if not _is_dry(r)]
    if strategy is not None:
        rows = [r for r in rows if getattr(r, "strategy", "") == strategy]

    start_dt = None
    if settings.perf_start_date:
        try:
            from datetime import timedelta as _td
            # PERF_START_DATE는 KST 기준 — DB는 UTC 저장이므로 9시간 빼서 비교
            kst_midnight = _dt.strptime(settings.perf_start_date, "%Y-%m-%d")
            start_dt = kst_midnight - _td(hours=9)
        except ValueError:
            pass

    total_realized = 0.0
    buy_count = 0
    sell_count = 0
    # 종목별 매수 큐: deque of [price, remaining_qty]
    buy_queues: dict[str, deque] = {}

    for r in rows:
        if start_dt and r.ts < start_dt:
            continue
        # 종목코드 정규화: 과거 데이터에 .KS 접미사 섞인 행이 있어도 같은 종목으로
        # FIFO 매칭되게 함 (005930 vs 005930.KS 분리로 매도 누락되던 버그 방지)
        sym = r.symbol.split(".")[0]
        if sym not in buy_queues:
            buy_queues[sym] = deque()
        if r.side == "buy":
            buy_count += 1
            buy_queues[sym].append([r.price, r.quantity])
        elif r.side == "sell":
            sell_count += 1
            remaining = r.quantity
            while remaining > 0 and buy_queues.get(sym):
                buy_price, buy_qty = buy_queues[sym][0]
                matched = min(remaining, buy_qty)
                gross = (r.price - buy_price) * matched
                # 모의투자: 증권거래세(0.18%) 미부과 → 수수료(0.015%)만 차감
                effective_sell_fee = settings.trade_fee_buy_pct if settings.is_paper else settings.trade_fee_sell_pct
                fee = (r.price * effective_sell_fee
                       + buy_price * settings.trade_fee_buy_pct) * matched
                total_realized += gross - fee
                remaining -= matched
                buy_queues[sym][0][1] -= matched
                if buy_queues[sym][0][1] <= 0:
                    buy_queues[sym].popleft()

    initial = settings.initial_capital_krw
    pnl_pct = (total_realized / initial * 100) if initial > 0 else None
    return {
        "realized_pnl": total_realized,
        "pnl_pct": pnl_pct,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_trades": buy_count + sell_count,
        "initial_capital": initial,
        "fee_buy_pct": settings.trade_fee_buy_pct * 100,
        "fee_sell_pct": (settings.trade_fee_buy_pct if settings.is_paper else settings.trade_fee_sell_pct) * 100,
        "is_paper": settings.is_paper,
    }


def _merge_positions_into_symbols(symbols_str: str) -> str:
    """스크리너/수동 저장 시 열린 포지션 종목이 SYMBOLS에서 빠지지 않도록 병합."""
    try:
        pos_syms = [p["symbol"] for p in (_live_positions() or []) if p.get("symbol")]
        if not pos_syms:
            return symbols_str
        sym_list = [s for s in symbols_str.split(",") if s.strip()]
        # 6자리 코드만 추출해서 비교 (005930.KS → 005930)
        existing_codes = {s.split(".")[0] for s in sym_list}
        added = []
        for ps in pos_syms:
            code = ps.split(".")[0]  # suffix 제거 → 6자리 통일
            if code not in existing_codes:
                sym_list.append(code)
                existing_codes.add(code)
                added.append(code)
        if added:
            logger.info("포지션 보유 종목 SYMBOLS에 유지: {}", added)
        return ",".join(sym_list)
    except Exception:
        return symbols_str


def _apply_strategy_split(perf: dict, positions: list[dict]) -> None:
    """perf 에 전략별 순손익(실현+미실현) 키를 채운다 (in-place).

    총손익 = 브로커 net_pnl(가장 정확). FIFO 실현손익은 거래로그 종목코드
    불일치(예: 005930 vs 005930.KS)로 부정확할 수 있어 단독으로는 못 믿는다.
    → 총손익을 net_pnl 에 고정하고, 대장주는 자기 거래(disjoint·소수 종목이라
      FIFO 가 깨끗)로 직접 집계, 스톡봇 = 총 - 대장주 잔차로 둬 합이 항상
      브로커 진실과 일치하게 한다.

    수익률은 각 전략의 배정 자본 대비: 대장주=예산, 스톡봇=초기-예산, 총=초기.
    """
    initial = settings.initial_capital_krw or 0.0
    leader_perf = _realized_pnl_summary(strategy="leader_pullback")
    # 대장주 미실현: 현재 보유분 중 대장주로 분류된 종목의 (현재가-평단)*수량 합
    leader_unreal = sum(
        (p["current"] - p["avg"]) * p["qty"]
        for p in positions
        if p.get("strategy") == "leader" and p.get("qty")
    )
    leader_net = leader_perf["realized_pnl"] + leader_unreal
    total_net = perf["net_pnl"] if perf.get("net_pnl_available") else perf.get("realized_pnl", 0.0)
    stock_net = total_net - leader_net
    # 전략별 원금(분모): 각각 별도 설정. 미설정 시 초기자금-예산 등으로 폴백.
    leader_cap = settings.leader_capital_krw or settings.leader_budget_krw or 0.0
    stock_cap = settings.stock_capital_krw or (
        (initial - leader_cap) if initial > 0 else 0.0
    )
    total_cap = (stock_cap + leader_cap) if (stock_cap or leader_cap) else initial
    perf["total_net"] = total_net
    perf["total_net_pct"] = (total_net / total_cap * 100) if total_cap > 0 else 0.0
    perf["leader_net"] = leader_net
    perf["leader_net_pct"] = (leader_net / leader_cap * 100) if leader_cap > 0 else 0.0
    # 완료 거래 수 = 청산(매도) 횟수. total_trades(매수+매도)는 1라운드를 2건으로 셈.
    perf["leader_trades"] = leader_perf["sell_count"]
    perf["stock_net"] = stock_net
    perf["stock_net_pct"] = (stock_net / stock_cap * 100) if stock_cap > 0 else 0.0
    # 전략별 원금(설정값) — 대시보드 입력칸 표시용
    perf["stock_capital"] = stock_cap
    perf["leader_capital"] = leader_cap
    # (구) 실현손익 호환 키 유지 — 기존 참조 안전망
    perf["leader_realized"] = leader_perf["realized_pnl"]
    perf["stock_realized"] = perf.get("realized_pnl", 0.0) - leader_perf["realized_pnl"]


def _leader_today() -> dict:
    """오늘 대장주 상태·바스켓을 읽기 전용으로 재구성 (브로커 호출 없음).

    leader_trader 와 동일한 70% 룰·settings.symbols 제외 로직을 적용하되
    data/leader_picks·leader_trade_state JSON 만 읽는다 (표시 전용 — 동작 불변).
    반환: {enabled, selected_at, status, basket[], holding|None, done|None, skipped{}}.
    """
    out: dict = {
        "enabled": bool(getattr(settings, "leader_trade_enabled", False)),
        "selected_at": None, "status": None,
        "basket": [], "holding": None, "done": None, "skipped": {},
    }
    try:
        import json as _j
        from stock_bot.live.leader_trader import _PICKS_DIR, _STATE_DIR, _bare
    except Exception:
        return out
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    # 상태
    try:
        st = _j.loads((_STATE_DIR / f"{today}.json").read_text(encoding="utf-8"))
    except Exception:
        st = {}
    out["status"] = st.get("status")
    out["skipped"] = st.get("skipped", {}) or {}
    if st.get("status") == "holding":
        out["holding"] = {k: st.get(k) for k in
                          ("symbol", "name", "rank", "qty", "entry", "ref", "stop", "tp", "entry_at")}
    elif st.get("status") == "done":
        out["done"] = {k: st.get(k) for k in
                       ("symbol", "name", "qty", "entry", "exit", "exit_at", "exit_reason", "net_pct")}
    # 바스켓 (picks + 70% 룰 + 자기 종목 제외)
    try:
        picks = _j.loads((_PICKS_DIR / f"{today}.json").read_text(encoding="utf-8"))
        out["selected_at"] = picks.get("selected_at")
        leaders = picks.get("leaders") or []
        if leaders:
            top3 = leaders[0].get("top3") or [{
                "rank": 1, "code": leaders[0]["code"],
                "name": leaders[0].get("name", ""),
                "change_pct": leaders[0].get("change_pct", 0)}]
            top3 = sorted(top3, key=lambda x: x.get("rank", 9))
            lead_chg = float(top3[0].get("change_pct", 0))
            thresh = lead_chg * settings.leader_top3_ratio
            basket = [top3[0]] + [m for m in top3[1:]
                                  if float(m.get("change_pct", 0)) >= thresh]
            own = {_bare(s) for s in settings.symbols}
            out["basket"] = [
                {"code": _bare(m["code"]), "name": m.get("name", ""),
                 "rank": m.get("rank", 1), "change_pct": float(m.get("change_pct", 0))}
                for m in basket if _bare(m["code"]) not in own
            ]
    except Exception:
        pass
    return out


def _leader_bare_codes() -> set[str]:
    """포지션·시세 구분용 — 오늘 대장주 바스켓·보유 종목 bare code 집합."""
    info = _leader_today()
    codes = {m["code"] for m in info["basket"]}
    for slot in ("holding", "done"):
        d = info.get(slot)
        if d and d.get("symbol"):
            codes.add(str(d["symbol"]).split(".")[0])
    return codes


def _classify_strategy(symbol: str, leader_codes: set[str]) -> str:
    """종목 → 전략 태그. stock(스톡봇)/leader(대장주)/other(기타)."""
    bare = str(symbol).split(".")[0]
    if bare in leader_codes:
        return "leader"
    if bare in {s.split(".")[0] for s in settings.symbols}:
        return "stock"
    return "other"


def _live_positions() -> list[dict] | None:
    """브로커에서 현재 잔고 조회.

    반환값으로 '조회 실패'와 '진짜 빈 잔고'를 구분한다:
      · []    → 조회 성공, 보유 종목 없음 (전부 매도된 정상 상태)
      · None  → 조회 실패(타임아웃/DNS/5xx 등). 호출측은 직전 캐시 유지 판단에 사용.
    이 둘을 똑같이 [] 로 뭉개면, 매도로 빈 상태 + 이후 조회 실패가 겹칠 때
    판 종목이 캐시 폴백으로 계속 보유 중처럼 보이는 버그가 났었다.
    """
    global _broker_instance
    try:
        broker = _get_broker()
        if broker is None:
            return None
        rows = broker.get_positions()
        leader_codes = _leader_bare_codes()
        return [
            {
                "symbol": r.get("pdno", ""),
                "name": r.get("prdt_name", ""),
                "qty": int(r.get("hldg_qty", 0) or 0),
                "avg": float(r.get("pchs_avg_pric", 0) or 0),
                "current": float(r.get("prpr", 0) or 0),
                "pl_pct": float(r.get("evlu_pfls_rt", 0) or 0),
                "strategy": _classify_strategy(r.get("pdno", ""), leader_codes),
            }
            for r in rows
            if int(r.get("hldg_qty", 0) or 0) > 0
        ]
    except Exception as exc:
        logger.info("positions fetch failed (likely no credentials): {}", exc)
        _broker_instance = None  # 에러 시 다음 호출에서 재생성
        return None  # 실패 — 빈 잔고([])와 구분


_ACCOUNT_CACHE: dict = {"at": 0.0, "data": None}
_ACCOUNT_CACHE_TTL = 25.0  # 초. 30초 폴링 주기보다 짧게 설정

_POSITIONS_CACHE: dict = {"at": 0.0, "data": None}
_POSITIONS_CACHE_TTL = 5.0  # 실시간 UI 폴링용 짧은 TTL

_broker_instance = None

def _get_broker():
    """KISBroker 싱글턴 반환. httpx.Client 를 재사용해 fd 누수 방지."""
    global _broker_instance
    if _broker_instance is None:
        try:
            from stock_bot.broker import KISBroker
            _broker_instance = KISBroker()
        except Exception:
            return None
    return _broker_instance


def _account_summary(force: bool = False, cache_only: bool = False) -> dict:
    """브로커에서 계좌 잔고 요약. 실패 시 0 채워진 dict.

    60초 TTL 메모리 캐시. force=True 면 무시하고 재조회.
    cache_only=True 면 브로커를 절대 호출하지 않고 캐시(없으면 blank)만 반환 —
    최초 HTML 렌더가 KIS 동기 호출로 막히지 않게 하는 용도(프론트가 폴링으로 갱신).
    대시보드 새로고침 도중 KIS 쿼터/429 남발 방지.
    """
    global _broker_instance  # except 절에서 싱글턴 무효화 → 다음 호출 재생성
    blank = {
        "deposit": 0.0,
        "stock_eval": 0.0,
        "total_eval": 0.0,
        "purchase": 0.0,
        "pnl": 0.0,
        "pnl_pct": 0.0,
        "available": False,
        "cached_age": 0,
    }
    now = time.time()
    cached = _ACCOUNT_CACHE["data"]
    age = now - _ACCOUNT_CACHE["at"]
    if cache_only:
        if cached is not None:
            out = dict(cached)
            out["cached_age"] = int(age)
            return out
        return blank
    if cached is not None and not force and age < _ACCOUNT_CACHE_TTL:
        out = dict(cached)
        out["cached_age"] = int(age)
        return out
    try:
        broker = _get_broker()
        if broker is None:
            return {**blank, "cached_age": 0}
        s = broker.get_account_summary()
        s["available"] = s.get("total_eval", 0) > 0 or s.get("deposit", 0) > 0
        # 실패/빈 응답(available=False) 은 캐시 오염 방지 위해 저장 안 함
        if s["available"]:
            _ACCOUNT_CACHE["data"] = s
            _ACCOUNT_CACHE["at"] = now
            out = dict(s)
            out["cached_age"] = 0
            return out
        # 이번 조회는 실패 — 이전 유효 캐시가 있으면 그것 사용
        if cached is not None:
            out = dict(cached)
            out["cached_age"] = int(age)
            return out
        return blank
    except Exception as exc:
        logger.info("account summary fetch failed (likely no credentials): {}", exc)
        _broker_instance = None  # 에러 시 다음 호출에서 재생성
        if cached is not None:
            out = dict(cached)
            out["cached_age"] = int(age)
            return out
        return blank
