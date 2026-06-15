"""틱 로그·거래 서술문 빌더 — 순수 표시(presentation) 계층.

runner.py 의 _tick 이 매 틱 호출하는 포매팅 전용 함수들을 모았다.
거래 로직(주문·게이트·사이징)은 일절 포함하지 않으며, Decision.meta 등
이미 계산된 값을 사람이 읽는 한국어 문자열로 변환만 한다.
동작 불변(behavior-preserving) 추출 — runner 가 import 해 그대로 사용한다.
"""
from __future__ import annotations

import pandas as pd

from stock_bot.config import settings
from stock_bot.names import get_name


_STRATEGY_KO = {
    "vwap": "VWAP",
    "supertrend": "Supertrend",
    "rsi": "RSI",
    "bollinger": "볼린저",
    "ema": "EMA크로스",
    "macd": "MACD",
    "momentum": "모멘텀",
    "daily_context": "장기보유청산",
}


def _build_tick_log(
    symbol: str,
    decision,
    closes: pd.Series,
    ohlcv_df: pd.DataFrame | None,
    *,
    ohlcv_df_hist: pd.DataFrame | None = None,
    orderbook: dict | None = None,
) -> str:
    """전략별 실제 수치를 포함한 상세 틱 로그 생성.

    각 전략별로 신호 방향과 함께 '왜 중립/마이너스인지' 이유를 표시.
    거래량은 필터 활성 여부와 무관하게 항상 현재봉/MA 비율 표시.
    orderbook 이 전달되면 매도/매수 호가창 5단계를 추가 표시.
    """
    import math as _math
    import re
    from stock_bot.strategy.rsi import _rsi

    meta = decision.meta
    votes = {v["name"]: v for v in meta.get("votes", [])}
    last = float(closes.iloc[-1])
    score = meta.get("weighted_score", 0)
    bv = meta.get("buy_votes", 0)
    sv = meta.get("sell_votes", 0)
    sig = decision.signal.value.upper()

    _SIG = {"buy": "▲매수", "sell": "▼매도", "hold": "─홀드"}

    parts: list[str] = []

    # ── VWAP ─────────────────────────────────────────────────────────
    if ohlcv_df is not None:
        _warmup = settings.trade_vwap_warmup_bars
        _df_calc = ohlcv_df.iloc[_warmup:] if len(ohlcv_df) > _warmup else ohlcv_df.iloc[0:0]
        if len(ohlcv_df) < _warmup:
            parts.append(f"VWAP 워밍업 중 ({len(ohlcv_df)}/{_warmup}봉)")
        if len(_df_calc) < 5:
            parts.append(f"VWAP 수집중 ({len(_df_calc)}/5봉)")
        else:
            try:
                tp = (_df_calc["high"] + _df_calc["low"] + _df_calc["close"]) / 3
                vol = _df_calc["volume"].replace(0, 1)
                vwap = float((tp * vol).cumsum().iloc[-1] / vol.cumsum().iloc[-1])
                dev = (last - vwap) / vwap * 100
                vwap_v = votes.get("vwap", {})
                vsig = _SIG.get(vwap_v.get("signal", "hold"), "─홀드")
                contrib = vwap_v.get("contrib", 0.0)
                parts.append(f"VWAP {vwap:,.0f}원 {dev:+.2f}% {vsig} ({contrib:+.3f})")
            except Exception:
                pass

    # ── Supertrend ────────────────────────────────────────────────────
    st_v = votes.get("supertrend", {})
    st_reason = st_v.get("reason", "")
    if "warmup" in st_reason or "봉부족" in st_reason:
        st_state = "수집중(봉부족)"
    elif "상승 전환" in st_reason:
        st_state = "하락→상승전환"
    elif "하락 전환" in st_reason:
        st_state = "상승→하락전환"
    elif "상승추세" in st_reason or "상승" in st_reason:
        st_state = "상승추세"
    elif "하락추세" in st_reason or "하락" in st_reason:
        st_state = "하락추세"
    else:
        st_state = "중립"
    vsig = _SIG.get(st_v.get("signal", "hold"), "─홀드")
    st_contrib = st_v.get("contrib", 0.0)
    parts.append(f"ST {st_state} {vsig} ({st_contrib:+.3f})")

    # ── RSI ───────────────────────────────────────────────────────────
    try:
        rsi_val = float(_rsi(closes, settings.trade_rsi_period).iloc[-1])
        rsi_v = votes.get("rsi", {})
        vsig = _SIG.get(rsi_v.get("signal", "hold"), "─홀드")
        contrib = rsi_v.get("contrib", 0.0)
        if _math.isnan(rsi_val):
            need = settings.trade_rsi_period + 1
            have = int(closes.notna().sum())
            parts.append(f"RSI 수집중({have}/{need}봉) {vsig} ({contrib:+.3f})")
        else:
            parts.append(
                f"RSI {rsi_val:.1f} "
                f"(기준 {settings.trade_rsi_oversold:.0f}/{settings.trade_rsi_overbought:.0f}) "
                f"{vsig} ({contrib:+.3f})"
            )
    except Exception:
        pass

    # ── Bollinger ─────────────────────────────────────────────────────
    try:
        bb_mid = float(closes.rolling(settings.trade_bb_window).mean().iloc[-1])
        bb_std = float(closes.rolling(settings.trade_bb_window).std().iloc[-1])
        bb_v = votes.get("bollinger", {})
        vsig = _SIG.get(bb_v.get("signal", "hold"), "─홀드")
        contrib = bb_v.get("contrib", 0.0)
        if _math.isnan(bb_mid) or _math.isnan(bb_std):
            need = settings.trade_bb_window
            have = int(closes.notna().sum())
            parts.append(f"BB 수집중({have}/{need}봉) {vsig} ({contrib:+.3f})")
        else:
            bb_upper = bb_mid + settings.trade_bb_k * bb_std
            bb_lower = bb_mid - settings.trade_bb_k * bb_std
            width = bb_upper - bb_lower
            pct = (last - bb_lower) / width if width > 0 else 0.5
            # 홀드 시: 밴드 내 현재가 위치를 시각적으로 표시
            if vsig == "─홀드":
                # pct 구간별 위치 설명 (0=하단, 0.5=중앙, 1=상단)
                _bar_len = 10
                _filled = min(int(pct * _bar_len), _bar_len - 1)
                _bar = "─" * _filled + "●" + "─" * (_bar_len - _filled - 1)
                if pct < 0.25:
                    pos_str = f"하단근접 [{_bar}] {pct*100:.0f}%"
                elif pct > 0.75:
                    pos_str = f"상단근접 [{_bar}] {pct*100:.0f}%"
                else:
                    pos_str = f"중간 [{_bar}] {pct*100:.0f}%"
                bb_info = f"  ← {pos_str}"
            else:
                bb_info = ""
            parts.append(
                f"BB {bb_lower:,.0f}~{bb_upper:,.0f}원 현재 {last:,.0f}원 {vsig} ({contrib:+.3f}){bb_info}"
            )
    except Exception:
        pass

    # ── DailyContext ──────────────────────────────────────────────────
    dc_v = votes.get("daily_context", {})
    dc_reason = dc_v.get("reason", "")
    dc_sig = dc_v.get("signal", "hold")
    dc_contrib = dc_v.get("contrib", 0.0)
    if "gate1" in dc_reason:
        dc_str = "DC  당일진입(보유1일미만 → 게이트1 미달)"
    elif "gate2" in dc_reason:
        m = re.search(r"수익[=]?([+-]?[\d.]+)%\s*<\s*([\d.]+)%", dc_reason)
        if m:
            dc_str = f"DC  수익{m.group(1)}% < {m.group(2)}%(게이트2 수익률 미달)"
        else:
            m2 = re.search(r"수익[=]?([+-]?[\d.]+)%", dc_reason)
            pct = m2.group(1) if m2 else "?"
            dc_str = f"DC  수익{pct}% < {settings.daily_context_profit_gate_pct}%(게이트2 수익률 미달)"
    elif "플로팅" in dc_reason or ("게이트 통과" in dc_reason):
        m = re.search(r"수익([+-]?[\d.]+)%", dc_reason)
        pct = m.group(1) if m else "?"
        cands = dc_reason.split("[")[-1].rstrip("]") if "[" in dc_reason else ""
        dc_str = f"DC  수익{pct}%(게이트통과) 플로팅미달[{cands}]"
    elif dc_sig == "sell":
        m = re.search(r"수익[=]?([+-]?[\d.]+)%", dc_reason)
        pct = m.group(1) if m else "?"
        m_cond = re.search(r"\[(.+)\]", dc_reason)
        cond_str = f" [{m_cond.group(1)}]" if m_cond else ""
        dc_str = f"DC  장기보유 청산 (수익{pct}%){cond_str}  ▼매도"
    else:
        dc_str = "DC  ─홀드  ← 미보유 또는 청산 조건 미해당"
    parts.append(f"{dc_str} ({dc_contrib:+.3f})")

    # ── 거래량 (항상 표시) ───────────────────────────────────────────
    # vol_filter_result: 필터 활성 시 계산된 결과 사용, 비활성 시 직접 계산
    vfr = meta.get("vol_filter_result", {})
    _vfr_action = vfr.get("action", "inactive") if vfr else "inactive"
    _vol_src = ohlcv_df_hist if (
        ohlcv_df_hist is not None and "volume" in ohlcv_df_hist.columns
    ) else ohlcv_df
    _vol_ma_period = settings.ensemble_volume_ma_period
    _vol_high_thr  = settings.ensemble_volume_high_ratio
    _vol_low_thr   = settings.ensemble_volume_low_ratio
    if _vfr_action not in ("inactive", "off") and vfr.get("ratio", 0) > 0:
        # 필터가 계산한 결과 표시
        _VFR_ICON = {
            "boost": "▲", "boost_sell": "▲↓",
            "penalty": "▼", "penalty_sell": "▼↑",
            "voter_buy": "투표↑", "voter_sell": "투표↓",
            "neutral": "〰",
        }
        action   = _vfr_action
        ratio    = vfr.get("ratio", 0.0)
        applied  = vfr.get("applied", 0.0)
        icon     = _VFR_ICON.get(action, "")
        if action == "neutral":
            vol_why = f"임계 미달 (기준 ≥{_vol_high_thr}/≤{_vol_low_thr}x) → 중립"
        elif action in ("boost", "penalty", "boost_sell", "penalty_sell"):
            vol_why = f"점수 조정 {applied:+.4f}"
        else:
            vol_why = f"투표 참여 {applied:+.4f}"
        _ma_used = vfr.get("ma_period_used", _vol_ma_period)
        _ma_label = f"MA{_ma_used}" if _ma_used == _vol_ma_period else f"MA{_ma_used}(봉부족,설정{_vol_ma_period})"
        parts.append(
            f"거래량  {_ma_label}대비 {ratio:.2f}x  {icon}({action}) ({applied:+.4f})"
            f"  ← {vol_why}"
        )
    elif _vol_src is not None and "volume" in _vol_src.columns and len(_vol_src) >= 5:
        # action=inactive: 필터 설정 여부와 무관하게 봉 수 부족으로 계산 못한 경우
        _vol_mode = vfr.get("mode", "off") if vfr else "off"
        try:
            _vol_s = _vol_src["volume"]
            _n = len(_vol_s)
            _ma_win = min(_vol_ma_period, _n - 1) if _n > 1 else 1
            _cur_vol = float(_vol_s.iloc[-1])
            _avg_vol = float(_vol_s.iloc[-1 - _ma_win:-1].mean()) if _ma_win >= 1 else _cur_vol
            if _avg_vol > 0:
                _ratio = _cur_vol / _avg_vol
                if _ratio >= _vol_high_thr:
                    _vol_comment = f"거래 활발 (≥{_vol_high_thr}x)"
                elif _ratio <= _vol_low_thr:
                    _vol_comment = f"거래 저조 (≤{_vol_low_thr}x)"
                else:
                    _vol_comment = "거래 보통"
                # 필터 활성인데 봉 부족 vs 필터 자체가 꺼진 경우 구분
                if _vol_mode in ("filter", "voter"):
                    _vol_label = f"필터 활성 중 (봉 부족 {_n}/{_vol_ma_period+1})"
                else:
                    _vol_label = "필터 OFF"
                parts.append(
                    f"거래량  {_cur_vol:,.0f}주  MA{_ma_win}대비 {_ratio:.2f}x  [{_vol_comment}]"
                    f"  ← {_vol_label}"
                )
        except Exception:
            pass

    # ── 뉴스 ─────────────────────────────────────────────────────────
    news_bias = meta.get("news_bias", 0)
    news_n = meta.get("news_article_count", 0)
    if news_n > 0:
        parts.append(f"뉴스  bias={news_bias:+.3f} ({news_n}건)")

    # ── 호가창 ────────────────────────────────────────────────────────
    if orderbook and (orderbook.get("asks") or orderbook.get("bids")):
        asks = orderbook.get("asks", [])   # [0]=매도1위(최우선)
        bids = orderbook.get("bids", [])   # [0]=매수1위(최우선)
        total_a = orderbook.get("total_ask_qty", 0)
        total_b = orderbook.get("total_bid_qty", 0)
        # 매도: 높은 가격이 5위, 낮은 가격(최우선)이 1위 → 위에서 아래로 5→1 역순 표시
        ask_lines: list[str] = []
        for idx, a in enumerate(reversed(asks[:5])):
            rank = len(asks[:5]) - idx
            marker = " ★" if rank == 1 else ""
            ask_lines.append(
                f"  매도{rank}  {a['price']:>8,.0f}원  {a['qty']:>7,}주{marker}"
            )
        bid_lines: list[str] = []
        for idx, b in enumerate(bids[:5]):
            rank = idx + 1
            marker = " ★" if rank == 1 else ""
            bid_lines.append(
                f"  매수{rank}  {b['price']:>8,.0f}원  {b['qty']:>7,}주{marker}"
            )
        # 총잔량 비율
        if total_a > 0 and total_b > 0:
            _ratio_str = f"  매도/매수 비 {total_a/total_b:.2f}x"
        else:
            _ratio_str = ""
        hoga_header = (
            f"┌─ 호가창  총매도 {total_a:,}주  /  총매수 {total_b:,}주{_ratio_str}"
        )
        hoga_body   = "\n    ".join(ask_lines + ["─" * 38] + bid_lines)
        parts.append(f"{hoga_header}\n    {hoga_body}")

    detail = "\n    ".join(parts)
    # ATR 손절 정보
    atr_str = ""
    if settings.atr_stop_loss_enabled or settings.position_sizing == "atr":
        _actual_stop = meta.get("effective_stop_pct", settings.trade_stop_loss_pct)
        atr_str = f" | 손절 -{_actual_stop:.2f}%(ATR)"
    _name = get_name(symbol) or ""
    _name_str = f" {_name}" if _name else ""
    header = (
        f"{symbol}{_name_str} [{settings.trade_strategy}] {sig} "
        f"score={score:+.2f} B{bv}/S{sv}"
        f" | 현재가 {last:,.0f}원{atr_str}"
    )
    return f"{header}\n    {detail}"


def _vote_sentence(name: str, reason: str, signal: str) -> str:
    """전략별 원시 reason → 한국어 한 줄 설명."""
    import re
    if name == "vwap":
        m = re.search(r'([+-][\d.]+)%', reason)
        pct = m.group(1) if m else "?"
        mv = re.search(r'vwap=([\d,]+)', reason)
        ref = mv.group(1) if mv else "?"
        if signal == "buy":
            return f"VWAP 기준({ref}원)보다 {pct}% 하락 이탈 → 평균회귀 매수"
        elif signal == "sell":
            return f"VWAP 기준({ref}원)보다 {pct}% 상승 이탈 → 차익실현"
        return f"VWAP 이탈 없음 (기준 {ref}원)"
    if name == "supertrend":
        if "상승 전환" in reason:
            return "하락→상승 추세 전환 감지 → 매수 신호"
        if "하락 전환" in reason:
            return "상승→하락 추세 전환 감지 → 매도 신호"
        if "상승추세" in reason:
            return "상승추세 유지 중, 진입 조건 미충족"
        if "하락추세" in reason:
            return "하락추세 유지 중, 매도 조건 미충족"
        return reason
    if name == "rsi":
        m = re.search(r'RSI\s*([\d.]+)', reason)
        val = float(m.group(1)) if m else None
        if signal == "buy" and val:
            return f"RSI {val:.1f} — 과매도 기준({settings.trade_rsi_oversold}) 하회, 반등 기대"
        if signal == "sell" and val:
            return f"RSI {val:.1f} — 과매수 기준({settings.trade_rsi_overbought}) 초과, 차익실현"
        return f"RSI {val:.1f} 중립 구간" if val else reason
    if name == "bollinger":
        if "lower rebound" in reason:
            return "볼린저 하단 이탈 후 재진입 → 과매도 반등 신호"
        if "lower turn" in reason:
            return "볼린저 하단 근처에서 2봉 연속 상승 → 반등 신호"
        if "upper revert" in reason:
            return "볼린저 상단 돌파 후 회귀 → 과매수 청산 신호"
        if "upper turn" in reason:
            return "볼린저 상단 근처에서 2봉 연속 하락 → 꺾임 신호"
        return "볼린저 밴드 중간 구간, 신호 없음"
    if name == "daily_context":
        if signal == "sell":
            return f"장기보유 청산 조건 충족 — {reason}"
        if "gate1 실패" in reason:
            return "장기보유 청산 미해당 (당일 진입 포지션)"
        if "gate2 실패" in reason:
            m = re.search(r"수익=([+-]?[\d.]+)%", reason)
            pct = m.group(1) if m else "?"
            return f"수익 {pct}% — 청산 임계(1.5%) 미달"
        if "플로팅 미달" in reason:
            return f"게이트 통과, 가격 조건 미달 ({reason.split('[')[-1].rstrip(']')})"
        return reason
    return reason


def _build_narrative(decision, side: str) -> str:
    """Decision.meta → 한국어 거래 서술문."""
    meta = decision.meta
    kind = meta.get("kind", "")

    if kind == "stop_loss":
        lp = meta.get("loss_pct", 0)
        ap = meta.get("avg_price", 0)
        cp = meta.get("last_price", 0)
        return (
            f"[손절] 평단 {ap:,.0f}원 → 현재 {cp:,.0f}원 ({lp:.2f}%)\n"
            f"손실 한도 초과로 강제 청산"
        )
    if kind == "news_critical_sell":
        ns = meta.get("news_sentiment", 0)
        nc = meta.get("news_critical_count", 0)
        return (
            f"[뉴스 긴급매도] 중요 기사 {nc}건 감지, 감성점수 {ns:+.2f}\n"
            f"포지션 즉시 청산"
        )
    if kind == "take_profit":
        pp = meta.get("profit_pct", 0)
        frac = meta.get("sell_fraction", 0)
        ap = meta.get("avg_price", 0)
        cp = meta.get("last_price", 0)
        thr = settings.take_profit_pct
        return (
            f"[분할익절] 수익 {pp:+.2f}% ≥ 목표 {thr:.1f}% 도달 → 보유분 {frac:.0%} 분할매도\n"
            f"평단 {ap:,.0f}원 → 현재 {cp:,.0f}원 (나머지 {1 - frac:.0%}는 계속 보유)"
        )

    votes = meta.get("votes", [])
    score = meta.get("weighted_score", 0)
    buy_v = meta.get("buy_votes", 0)
    sell_v = meta.get("sell_votes", 0)
    news_bias = meta.get("news_bias", 0)

    if not votes:
        return decision.reason

    lines = []
    for v in votes:
        name = v.get("name", "")
        sig = v.get("signal", "hold")
        raw = v.get("reason", "")
        contrib = v.get("contrib", 0.0)
        icon = "✅" if sig == "buy" else "🔴" if sig == "sell" else "⬜"
        label = _STRATEGY_KO.get(name, name.upper())
        score_str = f" ({contrib:+.3f})" if contrib != 0.0 else " (0.000)"
        lines.append(f"{icon} {label}{score_str}: {_vote_sentence(name, raw, sig)}")

    sr_adj = meta.get("sr_adj", 0.0)
    sr_tag = meta.get("sr_tag", "")
    if sr_tag:
        icon = "📍" if sr_adj > 0 else "🚧"
        lines.append(f"{icon} S/R: {sr_tag} (점수 {sr_adj:+.2f})")

    vfr = meta.get("vol_filter_result", {})
    if vfr and vfr.get("action", "inactive") not in ("inactive", "off"):
        _VFR_ICON = {
            "boost":       "📈",
            "boost_sell":  "📈↓",
            "penalty":     "📉",
            "penalty_sell":"📉↑",
            "voter_buy":   "🗳️↑",
            "voter_sell":  "🗳️↓",
            "neutral":     "〰️",
        }
        _ACTION_KO = {
            "boost": "상승 부스트", "boost_sell": "매도 강화",
            "penalty": "하락 패널티", "penalty_sell": "매도 완화",
            "voter_buy": "투표 매수", "voter_sell": "투표 매도",
        }
        action = vfr.get("action", "neutral")
        ratio = vfr.get("ratio", 0.0)
        applied = vfr.get("applied", 0.0)
        high_thr = vfr.get("high_thr", 1.2)
        low_thr = vfr.get("low_thr", 0.7)
        mode = vfr.get("mode", "")
        icon = _VFR_ICON.get(action, "🔢")
        thr_str = f"임계 ≥{high_thr}/≤{low_thr}"
        if action == "neutral":
            lines.append(f"{icon} 거래량: {ratio:.2f}x ({thr_str}) → 중립 (조정 없음)")
        else:
            lines.append(
                f"{icon} 거래량: {ratio:.2f}x [{thr_str}] "
                f"→ {_ACTION_KO.get(action, action)} (점수 {applied:+.4f}, 모드={mode})"
            )

    summary = f"종합점수 {score:+.2f} | 매수 {buy_v}표 / 매도 {sell_v}표"
    if abs(news_bias) > 0.005:
        direction = "긍정" if news_bias > 0 else "부정"
        summary += f" | 뉴스 {direction} 보정 {news_bias:+.3f}"

    lines.append(f"→ {summary}")
    return "\n".join(lines)
