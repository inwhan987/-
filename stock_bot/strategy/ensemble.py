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

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .bollinger import decide_bollinger
from .daily_context import decide_daily_context
from .ma_cross import Decision, MACrossSignal
from .rsi import decide_rsi
from .supertrend import decide_supertrend, _supertrend
from .vwap import decide_vwap
from stock_bot.market_calendar import KST as _KST

_SIGNAL_SCORE = {MACrossSignal.BUY: 1, MACrossSignal.HOLD: 0, MACrossSignal.SELL: -1}


@dataclass
class EnsembleConfig:
    # vwap, supertrend, rsi, bollinger, daily_context
    weights: tuple[float, ...] = (0.25, 0.22, 0.20, 0.18, 0.15)
    buy_threshold: float = 0.4
    sell_threshold: float = -0.3
    min_buy_votes: int = 2
    min_sell_votes: int = 2
    # VWAP
    vwap_band: float = 0.005              # 매수 이탈 기준
    vwap_sell_band: float | None = None  # 매도 이탈 기준 (None이면 vwap_band와 동일)
    vwap_st_bull_sell_band: float | None = None  # 슈퍼트렌드 상승추세 시 매도 기준 (None이면 vwap_sell_band 사용)
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
    # 라이브 전용: 마지막 봉이 방금 열린 부분봉(거래량 몇 초치뿐) → 직전 완성봉 기준으로 비교.
    # 백테스트는 완성봉만 들어오므로 False(기본) 유지 — 검증된 백테스트 동작 불변.
    volume_last_bar_partial: bool = False
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
    daily_context_trend_bonus: float = 0.5  # ST 상승 시 모든 PCT에 더하는 가산값
    # 1일 이상 보유 포지션 동적 임계값
    overnight_sell_threshold: float = -0.15
    overnight_min_sell_votes: int = 1
    # DC 동작 플래그 (튜닝/테스트용, 기본은 현재 동작 유지)
    dc_redistribute: bool = True       # False면 DC 비활성 시 가중치 재분배 안 함
    dc_force_sell_overnight: bool = False  # True면 오버나이트+DC SELL 시 즉시 청산
    # 추가매수 파라미터
    add_buy_enabled: bool = True
    add_buy_threshold: float = 0.45
    add_buy_min_votes: int = 2
    add_buy_require_trend_agree: bool = True  # ST가 하락추세면 추가매수 차단
    # Supertrend 방향 추적 (틱 간 전환 누락 방지)
    st_last_direction: int | None = None  # -1=하락, 1=상승
    # MACD (6번째 앙상블 전략, 기본 비활성)
    macd_enabled: bool = False
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal_period: int = 9
    macd_weight: float = 0.10
    # EMA 추세 방향 (7번째 앙상블 전략, 기본 비활성)
    # 크로스오버 순간이 아닌 EMA(fast)>EMA(slow) 구간 내내 BUY/SELL 출력
    ema_trend_enabled: bool = False
    ema_trend_fast: int = 9
    ema_trend_slow: int = 21
    ema_trend_weight: float = 0.15   # additive: 기존 가중치 합계에 더해짐
    # Donchian Channel (8번째, 기본 비활성)
    donchian_enabled: bool = False
    donchian_period: int = 20
    donchian_weight: float = 0.225
    # Linear Regression Slope (9번째, 기본 비활성)
    linreg_enabled: bool = False
    linreg_period: int = 30
    linreg_weight: float = 0.225
    # Parabolic SAR (10번째, 기본 비활성)
    psar_enabled: bool = False
    psar_step: float = 0.02
    psar_max_step: float = 0.2
    psar_weight: float = 0.225
    # KAMA (11번째, 기본 비활성)
    kama_enabled: bool = False
    kama_period: int = 10
    kama_fast: int = 2
    kama_slow: int = 30
    kama_weight: float = 0.225


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



def decide_ensemble(
    closes: pd.Series,
    ohlcv_df: pd.DataFrame | None = None,
    ohlcv_df_hist: pd.DataFrame | None = None,
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

    # ── 서브전략 2: Supertrend (VWAP sell_band 결정에 필요해 먼저 계산) ──
    _st_df = None
    if ohlcv_df_hist is not None and len(ohlcv_df_hist) >= cfg.supertrend_period + 2:
        _st_df = ohlcv_df_hist
    elif ohlcv_df is not None and len(ohlcv_df) >= cfg.supertrend_period + 2:
        _st_df = ohlcv_df
    if _st_df is not None:
        _, _st_dir_arr = _supertrend(_st_df, cfg.supertrend_period, cfg.supertrend_mult)
        _curr_st_dir = int(_st_dir_arr[-1])
        st_d = decide_supertrend(
            _st_df, cfg.supertrend_period, cfg.supertrend_mult,
            position_qty, avg_price, stop_loss_pct=999,
            prev_known_direction=cfg.st_last_direction,
        )
        cfg.st_last_direction = _curr_st_dir
    else:
        _curr_st_dir = None
        st_d = Decision(MACrossSignal.HOLD, "supertrend-warmup (봉부족)")

    # ── 서브전략 1: VWAP (오늘 봉만, 세션 기준 리셋) ─────────────────
    # 슈퍼트렌드 상승추세 유지 중이면 매도 임계값 상향 (추세 추종 중 조기 청산 방지)
    _vwap_sell = cfg.vwap_sell_band
    if _curr_st_dir == 1 and cfg.vwap_st_bull_sell_band is not None:
        _vwap_sell = cfg.vwap_st_bull_sell_band
    if ohlcv_df is not None:
        vwap_d = decide_vwap(
            ohlcv_df, cfg.vwap_band, position_qty, avg_price, stop_loss_pct=999,
            warmup_bars=cfg.vwap_warmup_bars, sell_band=_vwap_sell,
        )
    else:
        vwap_d = Decision(MACrossSignal.HOLD, "vwap-warmup (봉부족)")

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
        trend_bonus=cfg.daily_context_trend_bonus,
    )

    # ── DC 강제 청산 경로 (cfg.dc_force_sell_overnight=True 인 경우) ──────
    # 오버나이트 보유 중 DC 가 SELL 외치면 다른 신호/투표 무시하고 즉시 SELL
    # → DC 의 안전장치 역할 강화 (어제 산 거 오늘 정리)
    if cfg.dc_force_sell_overnight and _is_overnight(cfg, position_qty) and dc_d.signal is MACrossSignal.SELL:
        return Decision(MACrossSignal.SELL, "dc-force-sell", meta={"strategy": "daily_context", "kind": "dc_force"})

    # weights: 4개면 DailyContext 가중치 0으로 처리 (하위 호환)
    w = cfg.weights
    if len(w) < 5:
        w = (*w, 0.0)

    # DailyContext 제외 조건: 포지션 없거나 당일 진입 (오버나이트 아닌 경우)
    # → DC 가중치를 0으로 하고 나머지 4개에 비례 재분배 (cfg.dc_redistribute=True 인 경우)
    _dc_active = _is_overnight(cfg, position_qty)
    if not _dc_active:
        _dc_w = w[4]
        _base_sum = w[0] + w[1] + w[2] + w[3]
        if cfg.dc_redistribute and _base_sum > 0 and _dc_w > 0:
            _scale = (_base_sum + _dc_w) / _base_sum
            w = (w[0]*_scale, w[1]*_scale, w[2]*_scale, w[3]*_scale, 0.0)
        else:
            # 재분배 OFF: DC 가중치만 0 으로, 나머지 4개는 그대로 (총합 < 1)
            w = (w[0], w[1], w[2], w[3], 0.0)

    # ── 서브전략 6: MACD (선택적, 히스토그램 방향 기반) ──────────────────
    macd_d = None
    if cfg.macd_enabled:
        from .macd import decide_macd_ensemble
        macd_d = decide_macd_ensemble(closes, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal_period)

    # ── 서브전략 7: EMA 추세 방향 (선택적, EMA fast>slow=BUY 지속) ──────
    ema_trend_d = None
    if cfg.ema_trend_enabled:
        from .ema_cross import decide_ema_trend
        ema_trend_d = decide_ema_trend(closes, cfg.ema_trend_fast, cfg.ema_trend_slow)

    # ── 서브전략 8: Donchian Channel ────────────────────────────────────
    donchian_d = None
    if cfg.donchian_enabled:
        from .donchian import decide_donchian
        donchian_d = decide_donchian(closes, cfg.donchian_period)

    # ── 서브전략 9: Linear Regression Slope ─────────────────────────────
    linreg_d = None
    if cfg.linreg_enabled:
        from .linreg import decide_linreg
        linreg_d = decide_linreg(closes, cfg.linreg_period)

    # ── 서브전략 10: Parabolic SAR ───────────────────────────────────────
    psar_d = None
    if cfg.psar_enabled:
        _psar_src = ohlcv_df_hist if ohlcv_df_hist is not None else ohlcv_df
        if _psar_src is not None:
            from .psar import decide_psar
            psar_d = decide_psar(_psar_src, cfg.psar_step, cfg.psar_max_step)

    # ── 서브전략 11: KAMA ────────────────────────────────────────────────
    kama_d = None
    if cfg.kama_enabled:
        from .kama import decide_kama
        kama_d = decide_kama(closes, cfg.kama_period, cfg.kama_fast, cfg.kama_slow)

    sub_decisions = [
        ("vwap",          vwap_d, w[0]),
        ("supertrend",    st_d,   w[1]),
        ("rsi",           rsi_d,  w[2]),
        ("bollinger",     bb_d,   w[3]),
        ("daily_context", dc_d,   w[4]),
    ]
    # additive: 기존 가중치 그대로 + 추가 가중치 (합계 > 1.0 허용)
    if cfg.macd_enabled and macd_d is not None:
        sub_decisions.append(("macd", macd_d, cfg.macd_weight))
    if cfg.ema_trend_enabled and ema_trend_d is not None:
        sub_decisions.append(("ema_trend", ema_trend_d, cfg.ema_trend_weight))
    if cfg.donchian_enabled and donchian_d is not None:
        sub_decisions.append(("donchian", donchian_d, cfg.donchian_weight))
    if cfg.linreg_enabled and linreg_d is not None:
        sub_decisions.append(("linreg",   linreg_d,   cfg.linreg_weight))
    if cfg.psar_enabled and psar_d is not None:
        sub_decisions.append(("psar",     psar_d,     cfg.psar_weight))
    if cfg.kama_enabled and kama_d is not None:
        sub_decisions.append(("kama",     kama_d,     cfg.kama_weight))

    in_position = position_qty > 0
    # 포지션 보유 + 추가매수 OFF → BUY 신호를 score에서 완전히 제외
    # (BUY 기여가 SELL 기여를 상쇄해 청산 신호를 막는 문제 방지)
    _ignore_buy = in_position and not cfg.add_buy_enabled

    score = 0.0       # 주 점수 (add_buy ON 시 BUY 포함, OFF 시 BUY 제외)
    sell_score = 0.0  # 청산 전용 점수 — 항상 BUY 기여분 0 (포지션 보유 시 SELL 판단에 사용)
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
        s_no_buy = 0.0 if d.signal is MACrossSignal.BUY else s
        score += (s_no_buy if _ignore_buy else s) * weight
        sell_score += s_no_buy * weight
        # _ignore_buy 시 BUY 신호는 실제 점수에 반영 안 되므로 votes_detail도 동일하게 표시
        _eff_s = s_no_buy if _ignore_buy else s
        _eff_sig = (MACrossSignal.HOLD.value if (_ignore_buy and d.signal is MACrossSignal.BUY)
                    else d.signal.value)
        votes_detail.append(
            {"name": name, "signal": _eff_sig, "weight": weight,
             "contrib": round(_eff_s * weight, 4), "reason": d.reason}
        )
        if d.signal is MACrossSignal.BUY and not _ignore_buy:
            buy_votes += 1
            tags.append(f"{name}+")
        elif d.signal is MACrossSignal.SELL:
            sell_votes += 1
            tags.append(f"{name}-")

    # ── 거래량 modulator (가짜 돌파 필터) ──────────────────────────────
    volume_ratio = 0.0
    vol_filter_result: dict = {
        "ratio": 0.0,
        "high_thr": cfg.volume_high_ratio,
        "low_thr": cfg.volume_low_ratio,
        "applied": 0.0,
        "action": "inactive",
        "ma_period_used": cfg.volume_ma_period,
        "mode": (
            "filter" if cfg.volume_filter_enabled else
            "voter" if cfg.volume_as_voter_enabled else
            "off"
        ),
    }
    # 거래량 MA 소스: ohlcv_df_hist (어제 유사봉 + 오늘 실봉).
    # 어제 유사봉은 volume=0 이라 아래 vol>0 필터로 자동 제외 → 실질적으로 오늘 실봉만.
    # 라이브(volume_last_bar_partial)는 마지막 부분봉도 제외 — 방금 열린 봉의 몇 초치
    # 거래량을 MA25와 비교하면 항상 0.0x로 나와 필터가 죽어 있던 버그(2026-07-15).
    # 장초반 봉 부족은 아래 _eff_ma_period 축소로 대응.
    _vol_src = ohlcv_df_hist if (ohlcv_df_hist is not None and "volume" in ohlcv_df_hist.columns) else ohlcv_df
    volume_active = (
        (cfg.volume_filter_enabled or cfg.volume_buy_veto_enabled or cfg.volume_as_voter_enabled)
        and _vol_src is not None and "volume" in _vol_src.columns
    )
    _eff_ma_period = cfg.volume_ma_period
    if volume_active and len(_vol_src) >= 2:
        vol = _vol_src["volume"].astype(float)
        if cfg.volume_last_bar_partial:
            vol = vol.iloc[:-1]          # 부분봉 제외 → 직전 완성봉 기준
        vol = vol[vol > 0]               # 어제 유사봉(volume=0)·결측 제외
    else:
        vol = None
    if vol is not None and len(vol) >= 2:
        # 봉 수 부족 시 가용 봉으로 MA 기간 축소 (장 초반에도 필터 작동)
        _eff_ma_period = min(cfg.volume_ma_period, len(vol) - 1)
        vol_ma = vol.rolling(window=_eff_ma_period).mean()
        cur_vol = float(vol.iloc[-1])
        avg_vol = float(vol_ma.iloc[-1])
        if avg_vol > 0:
            volume_ratio = cur_vol / avg_vol
            vol_filter_result["ratio"] = round(volume_ratio, 3)
            vol_filter_result["action"] = "neutral"
            vol_filter_result["ma_period_used"] = _eff_ma_period

            # (1) 점수 조정 모드
            if cfg.volume_filter_enabled:
                _vol_delta = 0.0
                if score > 0:
                    if volume_ratio >= cfg.volume_high_ratio:
                        _vol_delta = cfg.volume_score_boost
                        vol_filter_result["action"] = "boost"
                        tags.append(f"vol+{volume_ratio:.1f}x")
                    elif volume_ratio <= cfg.volume_low_ratio:
                        _vol_delta = -cfg.volume_score_penalty
                        vol_filter_result["action"] = "penalty"
                        tags.append(f"vol-{volume_ratio:.1f}x")
                elif score < 0:
                    if volume_ratio >= cfg.volume_high_ratio:
                        _vol_delta = -cfg.volume_score_boost
                        vol_filter_result["action"] = "boost_sell"
                        tags.append(f"vol+{volume_ratio:.1f}x↓")
                    elif volume_ratio <= cfg.volume_low_ratio:
                        _vol_delta = cfg.volume_score_penalty
                        vol_filter_result["action"] = "penalty_sell"
                        tags.append(f"vol-{volume_ratio:.1f}x")
                if _vol_delta != 0.0:
                    score += _vol_delta
                    sell_score += _vol_delta
                    vol_filter_result["applied"] = round(_vol_delta, 4)

            # (2) 거래량 투표자 모드
            if cfg.volume_as_voter_enabled and len(closes) >= 2:
                price_up = closes.iloc[-1] > closes.iloc[-2]
                if volume_ratio >= cfg.volume_high_ratio:
                    if price_up:
                        score += cfg.volume_voter_weight
                        buy_votes += 1
                        vol_filter_result["applied"] = round(cfg.volume_voter_weight, 4)
                        vol_filter_result["action"] = "voter_buy"
                        tags.append("vol-vote+")
                    else:
                        score -= cfg.volume_voter_weight
                        sell_votes += 1
                        vol_filter_result["applied"] = round(-cfg.volume_voter_weight, 4)
                        vol_filter_result["action"] = "voter_sell"
                        tags.append("vol-vote-")

    # ── 뉴스 modulator ────────────────────────────────────────────────
    news_bias = 0.0
    news_tag = ""
    if _news_usable(cfg):
        news_bias = cfg.news_sentiment * cfg.news_weight
        score += news_bias
        sell_score += news_bias
        news_tag = (
            f"news={cfg.news_sentiment:+.2f}"
            f"{'*' if cfg.news_critical_count > 0 else ''}"
            f"(n={cfg.news_article_count})"
        )
        tags.append(news_tag)

    # ── 장기보유 포지션 동적 임계값 (DailyContext 완화 경로) ────────────────
    # 트리거 조건 (3개 AND):
    #   1) overnight: 어제 이전 진입 (1일 이상 보유)
    #   2) DailyContext SELL: 1.5% 수익 + VWAP/PDH/PDC 조건 1개 이상 만족
    #   3) Supertrend SELL: 하락전환 발생 (HOLD 면 미적용 → 추세 보존)
    # 트리거 시 완화값 적용:
    #   sell_threshold: cfg.sell_threshold (-0.55) → cfg.overnight_sell_threshold (-0.20)
    #   min_sell_votes: cfg.min_sell_votes (2) → cfg.overnight_min_sell_votes (1)
    # 의도: 익일 이상 보유 + 1.5% 수익 도달 + ST 하락전환 → 빠르게 익절 청산.
    # 봇 공식 매도 기준(-0.55, 2표)과 의도적 불일치 — 안전장치 역할.
    overnight = _is_overnight(cfg, position_qty)
    daily_context_sold = dc_d.signal is MACrossSignal.SELL
    supertrend_bearish = st_d.signal is MACrossSignal.SELL
    overnight_relaxed_active = overnight and daily_context_sold and supertrend_bearish
    if overnight_relaxed_active:
        effective_sell_threshold = cfg.overnight_sell_threshold
        effective_min_sell = cfg.overnight_min_sell_votes
        tags.append("overnight-relaxed")
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
        f"score={score:+.2f}"
        + (f" sell_score={sell_score:+.2f}" if in_position else "")
        + f" votes=B{buy_votes}/S{sell_votes}"
        f" [{' '.join(tags) or 'all hold'}]"
    )
    meta: dict = {
        "kind": "ensemble",
        "weighted_score": round(score, 4),
        "sell_score": round(sell_score, 4),
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
        "overnight_relaxed_active": overnight_relaxed_active,  # 완화 경로 발동 여부
        "overnight_sell_threshold": cfg.overnight_sell_threshold,
        "overnight_min_sell_votes": cfg.overnight_min_sell_votes,
        "vol_filter_result": vol_filter_result,
    }

    # ── 매수 판단 ────────────────────────────────────────────────────
    if score >= cfg.buy_threshold and buy_votes >= min_buy:
        if position_qty == 0:
            # 신규 매수
            if cfg.volume_buy_veto_enabled and volume_active and volume_ratio < cfg.volume_buy_veto_ratio:
                tags.append(f"buy-veto(vol{volume_ratio:.1f}x)")
                reason_veto = (
                    f"score={score:+.2f} votes=B{buy_votes}/S{sell_votes}"
                    f" [{' '.join(tags)}]"
                )
                return Decision(MACrossSignal.HOLD, reason_veto,
                               meta={**meta, "decision": "buy_veto_volume", "volume_ratio": round(volume_ratio, 2)})
            return Decision(MACrossSignal.BUY, reason, meta={**meta, "decision": "buy"})
        elif cfg.add_buy_enabled and score >= cfg.add_buy_threshold and buy_votes >= cfg.add_buy_min_votes:
            # ── 추가매수 추세 일치 요건: ST 하락추세 시 차단 ────
            if cfg.add_buy_require_trend_agree and _curr_st_dir == -1:
                return Decision(
                    MACrossSignal.HOLD,
                    f"[추가매수 차단] ST 하락추세 (score={score:+.2f}, B{buy_votes}/S{sell_votes})",
                    meta={**meta, "decision": "add_buy_veto_trend"},
                )
            # 추가매수 (더 높은 임계값 적용)
            add_reason = (
                f"[추가매수] score={score:+.2f} buy_votes={buy_votes} "
                f"(임계 {cfg.add_buy_threshold}/투표 {cfg.add_buy_min_votes})"
            )
            return Decision(
                MACrossSignal.BUY, add_reason,
                meta={**meta, "decision": "add_buy", "kind": "add_buy"},
            )

    # ── 매도 판단 ─────────────────────────────────────────────────────
    # 포지션 보유 시 sell_score 사용 (BUY 기여분 제거 → 매수 신호가 청산을 막지 않음)
    _chk_sell = sell_score if in_position else score

    # ── 워밍업 가드: RSI/BB/ST 3개 모두 유효해야 SELL 허용 ──────────────
    # 장 초반 데이터 부족으로 한 지표만 SELL 외치는 오신호 방지.
    # stop-loss 는 이 가드 위에서 처리되므로 영향 없음.
    _st_ready  = _st_df is not None                    # ST: 최소 supertrend_period+2봉
    _bb_ready  = len(closes) >= cfg.bb_window          # BB: 20봉 이상
    _rsi_ready = len(closes) >= cfg.rsi_period + 1     # RSI: 15봉 이상
    if in_position and not (_st_ready and _bb_ready and _rsi_ready):
        _not_ready = [n for n, ok in [("ST", _st_ready), ("BB", _bb_ready), ("RSI", _rsi_ready)] if not ok]
        return Decision(
            MACrossSignal.HOLD,
            f"SELL 차단: 워밍업 중 ({', '.join(_not_ready)})",
            meta={**meta, "decision": "sell_blocked_warmup",
                  "st_ready": _st_ready, "bb_ready": _bb_ready, "rsi_ready": _rsi_ready},
        )

    if in_position and _chk_sell <= effective_sell_threshold and sell_votes >= min_sell:
        # 완화 경로로 트리거된 매도는 reason 에 명시 (감사·디버그 추적용)
        if overnight_relaxed_active:
            # 봇 공식 매도 미충족이지만 완화 임계로 통과한 경우
            base_passed = _chk_sell <= cfg.sell_threshold and sell_votes >= cfg.min_sell_votes
            if not base_passed:
                sell_reason = (
                    f"[DailyContext 완화 경로 적용] "
                    f"sell_score={sell_score:+.2f} ≤ {effective_sell_threshold:+.2f} (완화), "
                    f"sell_votes={sell_votes} ≥ {effective_min_sell} (완화) | "
                    f"공식기준(-{abs(cfg.sell_threshold):.2f}/{cfg.min_sell_votes}표) 미충족 | "
                    f"트리거: overnight+DC SELL+ST SELL"
                )
                return Decision(MACrossSignal.SELL, sell_reason,
                               meta={**meta, "decision": "sell",
                                     "sell_path": "overnight_relaxed"})
        # 공식 기준 매도
        return Decision(MACrossSignal.SELL, reason,
                       meta={**meta, "decision": "sell", "sell_path": "standard"})

    return Decision(MACrossSignal.HOLD, reason, meta={**meta, "decision": "hold"})
