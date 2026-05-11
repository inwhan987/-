"""앙상블 전략: 5개 하위 전략 투표 + 뉴스 감성 modulator.

구성 (백테스트 기준 5분봉 최적):
  1. VWAP 평균회귀   (0.28) — 횡보장 주력
  2. Supertrend     (0.24) — 추세장 주력
  3. RSI 35/65      (0.16) — 과매도/과매수 필터
  4. Bollinger      (0.12) — 변동성 진입 확인
  5. DailyContext   (0.20) — 1일 이상 보유 포지션 청산 (SELL/HOLD 전용,
                             당일 장중 신호만 보는 나머지 전략의 공백을 보완)

기본 규칙 (뉴스 critical 기사 없을 때):
  매수: weighted_score >= buy_threshold AND buy_votes >= min_buy_votes
  매도: weighted_score <= sell_threshold AND sell_votes >= min_sell_votes

1일 이상 보유 포지션:
  동적 임계값 → sell_threshold = overnight_sell_threshold (-0.15)
              → min_sell_votes = overnight_min_sell_votes  (1)

뉴스는 투표에 참여하지 않고 다음 방식으로 영향:
  - weighted_score 에 `news_sentiment × news_weight` 합산 (어드바이저리)
  - critical 기사가 있으면 게이트 조정
  - 뉴스가 나쁘면 점수가 낮아져 매수 임계값 통과가 어려워지지만 하드 veto 없음
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd

from .bollinger import decide_bollinger
from .daily_context import decide_daily_context
from .ma_cross import Decision, MACrossSignal
from .rsi import decide_rsi
from .supertrend import decide_supertrend, _supertrend
from .vwap import decide_vwap

_KST = timezone(timedelta(hours=9))
_SIGNAL_SCORE = {MACrossSignal.BUY: 1, MACrossSignal.HOLD: 0, MACrossSignal.SELL: -1}


@dataclass
class EnsembleConfig:
    # vwap, supertrend, rsi, bollinger, daily_context
    weights: tuple[float, ...] = (0.28, 0.24, 0.16, 0.12, 0.20)
    buy_threshold: float = 0.4
    sell_threshold: float = -0.3
    min_buy_votes: int = 2
    min_sell_votes: int = 2
    # VWAP
    vwap_band: float = 0.005
    vwap_warmup_bars: int = 12  # 5분봉 1시간 — 동시호가 왜곡 방지
    # Supertrend
    supertrend_period: int = 7
    supertrend_mult: float = 3.0
    # RSI
    rsi_period: int = 14
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    # Bollinger
    bb_window: int = 20
    bb_k: float = 2.0
    bb_consec: int = 3   # 꺾임 감지 연속 봉 수 (2 or 3)
    # VWAP 매도 신호 강도 (1.0=대칭, <1.0=매도 약화 — 추세에서 일찍 빠짐 방지)
    vwap_sell_strength: float = 1.0
    # Volume filter (가짜 돌파 신호 필터)
    volume_filter_enabled: bool = False     # 점수 가산/감산 모드
    volume_buy_veto_enabled: bool = False   # 매수 거부권 (low volume이면 매수 금지)
    volume_as_voter_enabled: bool = False   # 거래량을 6번째 투표자로 사용
    volume_ma_period: int = 20
    volume_high_ratio: float = 1.2    # 평균 거래량 × 이 배수 이상이면 신호 강화
    volume_low_ratio: float = 0.7     # 평균 거래량 × 이 배수 이하면 신호 약화
    volume_buy_veto_ratio: float = 1.0  # 매수 시 거래량 ≥ 이 배수 필요 (veto 모드)
    volume_score_boost: float = 0.10  # 거래량 동반 시 점수 가산
    volume_score_penalty: float = 0.05  # 거래량 부족 시 점수 감산
    volume_voter_weight: float = 0.10  # 거래량 투표자 가중치
    # 뉴스 modulator (투표 아님)
    news_weight: float = 0.3
    news_sentiment: float | None = None
    news_article_count: int = 0
    news_critical_count: int = 0
    news_strong_neg_count: int = 0  # sentiment_score <= news_veto_threshold 인 기사 수
    news_strong_neg_ratio: float = 0.10  # 강한 부정 기사 비율 ≥ 이 값이면 매수 veto
    news_min_articles: int = 3
    news_veto_threshold: float = -0.4
    news_escalate_buy: float = 0.5
    news_escalate_sell: float = -0.5
    # DailyContext (5번째 전략) 파라미터
    daily_context_entry_date: str | None = None      # "YYYY-MM-DD" KST
    daily_context_prev_day_high: float = 0.0
    daily_context_prev_day_close: float = 0.0
    daily_context_profit_gate_pct: float = 1.5
    daily_context_avwap_pct: float = 1.5
    daily_context_pdh_pct: float = 1.0
    daily_context_pdc_pct: float = 1.5
    # 1일 이상 보유 포지션 동적 임계값
    overnight_sell_threshold: float = -0.15
    overnight_min_sell_votes: int = 1
    # 추가매수 파라미터
    add_buy_enabled: bool = True
    add_buy_threshold: float = 0.60      # 기본 매수(0.40)보다 높게
    add_buy_min_votes: int = 3           # 기본 매수(2)보다 엄격
    # Supertrend 방향 추적 (틱 간 전환 누락 방지)
    st_last_direction: int | None = None  # -1=하락, 1=상승


def _news_usable(cfg: EnsembleConfig) -> bool:
    return (
        cfg.news_weight > 0
        and cfg.news_sentiment is not None
        and cfg.news_article_count >= cfg.news_min_articles
    )


def _is_overnight(cfg: EnsembleConfig, position_qty: int) -> bool:
    if position_qty <= 0 or cfg.daily_context_entry_date is None:
        return False
    today_str = datetime.now(tz=_KST).strftime("%Y-%m-%d")
    return cfg.daily_context_entry_date < today_str


def _eval_buy_signals(
    closes: pd.Series,
    ohlcv_df: pd.DataFrame | None,
    cfg: "EnsembleConfig",
    news_bias: float,
) -> tuple[float, int, list[dict]]:
    """추가매수 전용: position_qty=0 기준으로 BUY 신호 재평가.

    Returns (score, buy_votes, votes_detail)
    """
    # position_qty=0 으로 호출 → 서브전략의 포지션 게이트 우회
    if ohlcv_df is not None and len(ohlcv_df) >= cfg.supertrend_period + 2:
        vwap_d = decide_vwap(ohlcv_df, cfg.vwap_band, 0, 0.0, stop_loss_pct=999, warmup_bars=cfg.vwap_warmup_bars)
        st_d = decide_supertrend(ohlcv_df, cfg.supertrend_period, cfg.supertrend_mult, 0, 0.0, stop_loss_pct=999)
    else:
        from .rsi import _rsi
        rsi_val = float(_rsi(closes, cfg.rsi_period).iloc[-1])
        vwap_d = Decision(
            MACrossSignal.BUY if rsi_val < cfg.rsi_oversold else
            MACrossSignal.SELL if rsi_val > cfg.rsi_overbought else MACrossSignal.HOLD,
            f"vwap-fallback RSI {rsi_val:.1f}",
        )
        st_d = Decision(MACrossSignal.HOLD, "supertrend-fallback")
    rsi_d = decide_rsi(closes, cfg.rsi_period, cfg.rsi_oversold, cfg.rsi_overbought, 0, 0.0, stop_loss_pct=999)
    bb_d = decide_bollinger(closes, cfg.bb_window, cfg.bb_k, 0, 0.0, stop_loss_pct=999, consec=cfg.bb_consec)

    w = cfg.weights if len(cfg.weights) >= 4 else (*cfg.weights, 0.0)
    # DailyContext는 추가매수 평가에서 제외 (SELL/HOLD 전용)
    subs = [("vwap", vwap_d, w[0]), ("supertrend", st_d, w[1]), ("rsi", rsi_d, w[2]), ("bollinger", bb_d, w[3])]

    score = news_bias
    buy_votes = 0
    detail: list[dict] = []
    for name, d, weight in subs:
        s = _SIGNAL_SCORE[d.signal]
        score += s * weight
        detail.append({"name": name, "signal": d.signal.value, "weight": weight})
        if d.signal is MACrossSignal.BUY:
            buy_votes += 1
    return score, buy_votes, detail


def decide_ensemble(
    closes: pd.Series,
    ohlcv_df: pd.DataFrame | None = None,
    position_qty: int = 0,
    avg_price: float = 0.0,
    stop_loss_pct: float = 5.0,
    config: EnsembleConfig | None = None,
) -> Decision:
    """앙상블 의사결정.

    ohlcv_df: high/low/close/volume 컬럼을 포함한 DataFrame (오래된→최신 순).
              None 이면 closes-only 폴백으로 동작.
    """
    cfg = config or EnsembleConfig()
    if len(closes) < 1:
        return Decision(MACrossSignal.HOLD, "no closes data")

    last_price = float(closes.iloc[-1])

    # ── 손절 (최우선, 봉수 무관 항상 동작) ────────────────────────────
    # 각 서브전략의 데이터 부족과 별개로, 손절은 last_price/avg_price 만 있으면 가능
    if position_qty > 0 and avg_price > 0:
        loss_pct = (last_price - avg_price) / avg_price * 100
        if loss_pct <= -abs(stop_loss_pct):
            return Decision(
                MACrossSignal.SELL,
                f"stop-loss {loss_pct:.2f}%",
                meta={
                    "kind": "stop_loss",
                    "loss_pct": loss_pct,
                    "avg_price": avg_price,
                    "last_price": last_price,
                },
            )

    # ── 서브전략 1~4: 분봉 기반 ───────────────────────────────────────
    if ohlcv_df is not None and len(ohlcv_df) >= cfg.supertrend_period + 2:
        vwap_d = decide_vwap(
            ohlcv_df, cfg.vwap_band, position_qty, avg_price, stop_loss_pct=999,
            warmup_bars=cfg.vwap_warmup_bars,
        )
        _, _st_dir_arr = _supertrend(ohlcv_df, cfg.supertrend_period, cfg.supertrend_mult)
        _curr_st_dir = int(_st_dir_arr[-1])
        st_d = decide_supertrend(
            ohlcv_df, cfg.supertrend_period, cfg.supertrend_mult,
            position_qty, avg_price, stop_loss_pct=999,
            prev_known_direction=cfg.st_last_direction,
        )
        cfg.st_last_direction = _curr_st_dir
    else:
        from .rsi import _rsi
        rsi_val = float(_rsi(closes, cfg.rsi_period).iloc[-1])
        vwap_d = Decision(
            MACrossSignal.BUY if rsi_val < cfg.rsi_oversold else
            MACrossSignal.SELL if rsi_val > cfg.rsi_overbought else MACrossSignal.HOLD,
            f"vwap-fallback RSI {rsi_val:.1f}",
        )
        st_d = Decision(MACrossSignal.HOLD, "supertrend-fallback (no ohlcv)")

    rsi_d = decide_rsi(
        closes, cfg.rsi_period, cfg.rsi_oversold, cfg.rsi_overbought,
        position_qty, avg_price, stop_loss_pct=999,
    )

    bb_d = decide_bollinger(
        closes, cfg.bb_window, cfg.bb_k,
        position_qty, avg_price, stop_loss_pct=999, consec=cfg.bb_consec,
    )

    # ── 서브전략 5: DailyContext (SELL/HOLD 전용) ─────────────────────
    # Supertrend 방향 판단: BUY 신호 or "상승" 유지 = 상승추세
    from .ma_cross import MACrossSignal as _MCS
    _st_bullish: bool | None = None
    if st_d.signal == _MCS.BUY or "상승" in st_d.reason:
        _st_bullish = True
    elif st_d.signal == _MCS.SELL or "하락" in st_d.reason:
        _st_bullish = False

    dc_d = decide_daily_context(
        ohlcv_df=ohlcv_df,
        position_qty=position_qty,
        avg_price=avg_price,
        entry_date=cfg.daily_context_entry_date,
        prev_day_high=cfg.daily_context_prev_day_high,
        prev_day_close=cfg.daily_context_prev_day_close,
        profit_gate_pct=cfg.daily_context_profit_gate_pct,
        avwap_pct=cfg.daily_context_avwap_pct,
        pdh_pct=cfg.daily_context_pdh_pct,
        pdc_pct=cfg.daily_context_pdc_pct,
        supertrend_bullish=_st_bullish,
    )

    # weights: 4개면 DailyContext 가중치 0으로 처리 (하위 호환)
    w = cfg.weights
    if len(w) < 5:
        w = (*w, 0.0)

    sub_decisions = [
        ("vwap",          vwap_d, w[0]),
        ("supertrend",    st_d,   w[1]),
        ("rsi",           rsi_d,  w[2]),
        ("bollinger",     bb_d,   w[3]),
        ("daily_context", dc_d,   w[4]),
    ]

    score = 0.0
    buy_votes = 0
    sell_votes = 0
    tags: list[str] = []
    votes_detail: list[dict] = []
    for name, d, weight in sub_decisions:
        s = _SIGNAL_SCORE[d.signal]
        # VWAP 매도 신호 비대칭: 매수는 그대로, 매도만 강도 조절
        # 추세 상승 중 VWAP 위 잠시 통과 → 일찍 빠지는 문제 방지
        if name == "vwap" and d.signal is MACrossSignal.SELL and cfg.vwap_sell_strength != 1.0:
            s = s * cfg.vwap_sell_strength
        score += s * weight
        votes_detail.append(
            {"name": name, "signal": d.signal.value, "weight": weight, "reason": d.reason}
        )
        if d.signal is MACrossSignal.BUY:
            buy_votes += 1
            tags.append(f"{name}+")
        elif d.signal is MACrossSignal.SELL:
            sell_votes += 1
            tags.append(f"{name}-")

    # ── 거래량 modulator (가짜 돌파 필터) ──────────────────────────────
    volume_ratio = 0.0
    volume_active = (
        (cfg.volume_filter_enabled or cfg.volume_buy_veto_enabled or cfg.volume_as_voter_enabled)
        and ohlcv_df is not None and "volume" in ohlcv_df.columns
    )
    if volume_active and len(ohlcv_df) >= cfg.volume_ma_period + 1:
        vol = ohlcv_df["volume"]
        vol_ma = vol.rolling(window=cfg.volume_ma_period).mean()
        cur_vol = float(vol.iloc[-1])
        avg_vol = float(vol_ma.iloc[-1])
        if avg_vol > 0:
            volume_ratio = cur_vol / avg_vol

            # (1) 점수 조정 모드
            if cfg.volume_filter_enabled:
                if score > 0:
                    if volume_ratio >= cfg.volume_high_ratio:
                        score += cfg.volume_score_boost
                        tags.append(f"vol+{volume_ratio:.1f}x")
                    elif volume_ratio <= cfg.volume_low_ratio:
                        score -= cfg.volume_score_penalty
                        tags.append(f"vol-{volume_ratio:.1f}x")
                elif score < 0:
                    if volume_ratio >= cfg.volume_high_ratio:
                        score -= cfg.volume_score_boost
                        tags.append(f"vol+{volume_ratio:.1f}x↓")
                    elif volume_ratio <= cfg.volume_low_ratio:
                        score += cfg.volume_score_penalty
                        tags.append(f"vol-{volume_ratio:.1f}x")

            # (2) 거래량 투표자 모드
            if cfg.volume_as_voter_enabled and len(closes) >= 2:
                price_up = closes.iloc[-1] > closes.iloc[-2]
                if volume_ratio >= cfg.volume_high_ratio:
                    if price_up:
                        score += cfg.volume_voter_weight
                        buy_votes += 1
                        tags.append(f"vol-vote+")
                    else:
                        score -= cfg.volume_voter_weight
                        sell_votes += 1
                        tags.append(f"vol-vote-")

    # ── 뉴스 modulator ────────────────────────────────────────────────
    news_bias = 0.0
    news_tag = ""
    if _news_usable(cfg):
        news_bias = cfg.news_sentiment * cfg.news_weight
        score += news_bias
        news_tag = (
            f"news={cfg.news_sentiment:+.2f}"
            f"{'*' if cfg.news_critical_count > 0 else ''}"
            f"(n={cfg.news_article_count})"
        )
        tags.append(news_tag)

    # ── 장기보유 포지션 동적 임계값 ───────────────────────────────────────
    # DailyContext SELL + Supertrend 하락전환(SELL) 동시에 일치할 때만 완화된 임계값 적용.
    # Supertrend가 아직 상승추세 유지(HOLD) 중이면 기본 임계값 유지 → 섣부른 청산 방지.
    overnight = _is_overnight(cfg, position_qty)
    daily_context_sold = dc_d.signal is MACrossSignal.SELL
    supertrend_bearish = st_d.signal is MACrossSignal.SELL
    if overnight and daily_context_sold and supertrend_bearish:
        effective_sell_threshold = cfg.overnight_sell_threshold
        effective_min_sell = cfg.overnight_min_sell_votes
    else:
        effective_sell_threshold = cfg.sell_threshold
        effective_min_sell = cfg.min_sell_votes

    min_buy = cfg.min_buy_votes
    min_sell = effective_min_sell
    has_critical = cfg.news_critical_count > 0

    # 뉴스 critical 처리
    if _news_usable(cfg) and has_critical:
        if cfg.news_sentiment >= cfg.news_escalate_buy:
            min_buy = max(1, cfg.min_buy_votes - 1)
            tags.append("news-boost-buy")
        if cfg.news_sentiment <= cfg.news_escalate_sell and position_qty > 0:
            reason = (
                f"news-critical SELL: score={score:+.2f} {news_tag} "
                f"votes=B{buy_votes}/S{sell_votes} [{' '.join(tags)}]"
            )
            return Decision(
                MACrossSignal.SELL, reason,
                meta={
                    "kind": "news_critical_sell",
                    "weighted_score": round(score, 4),
                    "buy_votes": buy_votes, "sell_votes": sell_votes,
                    "votes": votes_detail,
                    "news_sentiment": cfg.news_sentiment,
                    "news_article_count": cfg.news_article_count,
                    "news_critical_count": cfg.news_critical_count,
                    "last_price": last_price,
                },
            )

    reason = (
        f"score={score:+.2f} votes=B{buy_votes}/S{sell_votes}"
        f" [{' '.join(tags) or 'all hold'}]"
    )
    meta: dict = {
        "kind": "ensemble",
        "weighted_score": round(score, 4),
        "buy_votes": buy_votes, "sell_votes": sell_votes,
        "min_buy": min_buy, "min_sell": min_sell,
        "buy_threshold": cfg.buy_threshold,
        "sell_threshold": effective_sell_threshold,
        "votes": votes_detail,
        "news_sentiment": cfg.news_sentiment,
        "news_article_count": cfg.news_article_count,
        "news_critical_count": cfg.news_critical_count,
        "news_strong_neg_count": cfg.news_strong_neg_count,
        "news_usable": _news_usable(cfg),
        "news_bias": round(news_bias, 4),
        "last_price": last_price,
        "overnight": overnight,
        "daily_context_sold": daily_context_sold,
    }

    # ── 신규 매수 판단 (포지션 없을 때) ──────────────────────────────
    if (
        position_qty == 0
        and score >= cfg.buy_threshold
        and buy_votes >= min_buy
    ):
        # 매수 거부권: 거래량 부족 시 매수 차단
        if cfg.volume_buy_veto_enabled and volume_active and volume_ratio < cfg.volume_buy_veto_ratio:
            tags.append(f"buy-veto(vol{volume_ratio:.1f}x)")
            reason_veto = (
                f"score={score:+.2f} votes=B{buy_votes}/S{sell_votes}"
                f" [{' '.join(tags)}]"
            )
            return Decision(MACrossSignal.HOLD, reason_veto,
                           meta={**meta, "decision": "buy_veto_volume", "volume_ratio": round(volume_ratio, 2)})
        return Decision(MACrossSignal.BUY, reason, meta={**meta, "decision": "buy"})

    # ── 추가매수 판단 (포지션 있을 때, position_qty=0 으로 재평가) ────
    # 서브전략들이 position_qty>0 이면 BUY 신호를 내지 않으므로
    # 추가매수 전용으로 position_qty=0 기준으로 신호를 재계산
    if position_qty > 0 and cfg.add_buy_enabled:
        add_score, add_buy_votes, add_votes_detail = _eval_buy_signals(
            closes, ohlcv_df, cfg, news_bias
        )
        if add_score >= cfg.add_buy_threshold and add_buy_votes >= cfg.add_buy_min_votes:
            add_reason = (
                f"[추가매수] score={add_score:+.2f} buy_votes={add_buy_votes} "
                f"(임계 {cfg.add_buy_threshold}/투표 {cfg.add_buy_min_votes})"
            )
            return Decision(
                MACrossSignal.BUY, add_reason,
                meta={
                    **meta,
                    "decision": "add_buy", "kind": "add_buy",
                    "add_buy_score": round(add_score, 4),
                    "add_buy_votes": add_buy_votes,
                    "add_buy_votes_detail": add_votes_detail,
                },
            )

    # ── 매도 판단 ─────────────────────────────────────────────────────
    if position_qty > 0 and score <= effective_sell_threshold and sell_votes >= min_sell:
        return Decision(MACrossSignal.SELL, reason, meta={**meta, "decision": "sell"})

    return Decision(MACrossSignal.HOLD, reason, meta={**meta, "decision": "hold"})
