"""Environment-driven configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 리포지토리 루트의 .env 를 절대 경로로 고정. 작업 디렉토리 바뀌어도 일관되게 읽힘.
_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILES = tuple(
    str(p) for p in (_ROOT / ".env", _ROOT / ".env.overrides") if p.exists()
) or (str(_ROOT / ".env"),)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kis_app_key: str = Field(default="")
    kis_app_secret: str = Field(default="")
    kis_account_no: str = Field(default="")
    kis_env: Literal["paper", "real"] = Field(default="paper")

    discord_webhook_url: str = Field(default="")

    trade_symbols: str = Field(default="005930")
    trade_cash_per_trade: int = Field(default=500_000)
    trade_stop_loss_pct: float = Field(default=5.0)
    trade_short_ma: int = Field(default=5)
    trade_long_ma: int = Field(default=20)

    # 주문을 실제로 내지 않고 로그만 남김 (실전 전환 전 필수 검증 단계)
    trade_dry_run: bool = Field(default=False)
    # 전략 선택
    trade_strategy: Literal[
        "ma_cross", "rsi", "macd", "bollinger", "ensemble", "news",
        "ema_cross", "momentum",
    ] = Field(default="ma_cross")

    # 앙상블 파라미터
    ensemble_weights: str = Field(default="0.25,0.22,0.20,0.18,0.15")  # vwap,supertrend,rsi,bollinger,daily_context
    ensemble_buy_threshold: float = Field(default=0.4)
    ensemble_sell_threshold: float = Field(default=-0.3)
    ensemble_min_buy_votes: int = Field(default=2)   # 5개 중 2개 동의
    ensemble_min_sell_votes: int = Field(default=2)
    # 1일 이상 보유 포지션 동적 매도 임계값 (당일 진입 제외)
    overnight_sell_threshold: float = Field(default=-0.15)
    overnight_min_sell_votes: int = Field(default=1)

    # VWAP 파라미터 (앙상블 서브전략 1)
    trade_vwap_band: float = Field(default=0.007)               # 매수 이탈 기준 (0.7%)
    trade_vwap_sell_band: float = Field(default=0.0)            # 매도 이탈 기준 (0.0=vwap_band와 동일)
    trade_vwap_st_bull_sell_band: float = Field(default=0.0)    # 슈퍼트렌드 상승추세 시 매도 기준 (0.0=vwap_sell_band와 동일)
    trade_vwap_warmup_bars: int = Field(default=12)             # 5분봉 1시간(12봉) — 동시호가 왜곡 방지

    # Supertrend 파라미터 (앙상블 서브전략 2)
    trade_supertrend_period: int = Field(default=5)
    trade_supertrend_mult: float = Field(default=3.0)

    # EMA 크로스 파라미터 (5분봉 기준 9/21)
    trade_ema_fast: int = Field(default=9)
    trade_ema_slow: int = Field(default=21)

    # RSI 파라미터
    trade_rsi_period: int = Field(default=21)
    trade_rsi_oversold: float = Field(default=30.0)
    trade_rsi_overbought: float = Field(default=72.0)

    # MACD 파라미터 (5분봉 최적: 5/13/4)
    trade_macd_fast: int = Field(default=5)
    trade_macd_slow: int = Field(default=13)
    trade_macd_signal: int = Field(default=4)

    # 모멘텀(ROC) 파라미터
    trade_momentum_period: int = Field(default=10)
    trade_momentum_threshold: float = Field(default=0.0)

    # Bollinger 파라미터
    trade_bb_window: int = Field(default=15)
    trade_bb_k: float = Field(default=1.7)

    # 거래량 필터 (가짜 돌파 신호 차단 — 점수 가산/감산 모드)
    ensemble_volume_filter_enabled: bool = Field(default=False)
    ensemble_volume_ma_period: int = Field(default=20)
    ensemble_volume_high_ratio: float = Field(default=1.2)
    ensemble_volume_low_ratio: float = Field(default=0.7)
    ensemble_volume_score_boost: float = Field(default=0.10)
    ensemble_volume_score_penalty: float = Field(default=0.05)

    # 신규 진입 시간대 차단 (장초반 변동성 회피)
    # HH:MM 형식. 이 시간대 동안 BUY는 무조건 차단,
    # SELL은 수익률 ≥ entry_block_min_profit_to_sell_pct 일 때만 통과 (잡신호 회피).
    # 단, kind="stop_loss" SELL 은 항상 통과 (큰 손실 컷)
    entry_block_enabled: bool = Field(default=False)
    entry_block_start: str = Field(default="09:00")
    entry_block_end: str = Field(default="09:40")
    entry_block_min_profit_to_sell_pct: float = Field(default=3.0)
    # 강제매도 분할 비율 (0.5 = 50% 매도 후 잔량 유지, 1.0 = 전량 매도)
    entry_block_force_sell_fraction: float = Field(default=0.5)

    # 포지션 사이징
    position_sizing: Literal["fixed", "fraction", "atr"] = Field(default="fixed")
    position_fraction: float = Field(default=0.4)   # 40% of account
    risk_per_trade_pct: float = Field(default=1.0)  # ATR 모드: 한 번에 계좌 1% 리스크
    atr_period: int = Field(default=14)
    atr_stop_multiplier: float = Field(default=2.0)
    atr_stop_max_pct: float = Field(default=5.0)   # ATR 손절 상한 캡 (%)
    # ATR 동적 손절 단독 활성화 (포지션 사이징 모드와 무관)
    atr_stop_loss_enabled: bool = Field(default=False)
    max_position_pct: float = Field(default=30.0)   # 한 종목 최대 계좌 대비 %
    # 계좌 총액 (ATR/fraction 모드에서 쓰임). 0 이면 브로커 잔고에서 조회 시도.
    account_size_krw: float = Field(default=0.0)

    # 웹 대시보드
    web_host: str = Field(default="0.0.0.0")
    web_port: int = Field(default=8000)

    # 실시간 러너 주기 (분) / 캔들 소스
    live_interval_minutes: int = Field(default=15)
    # "daily" | "minute"
    live_candle: Literal["daily", "minute"] = Field(default="daily")
    live_minute_interval: int = Field(default=5)  # 5/10/30/60 지원

    # Prometheus metrics 서버 포트 (0 이면 비활성)
    metrics_port: int = Field(default=0)

    # 뉴스 크롤링
    news_enabled: bool = Field(default=False)
    news_crawl_interval_minutes: int = Field(default=60)
    news_pages_per_symbol: int = Field(default=1)
    news_lookback_hours: int = Field(default=24)
    news_min_articles: int = Field(default=3)
    news_buy_threshold: float = Field(default=0.3)
    news_sell_threshold: float = Field(default=-0.3)
    news_prefer_llm: bool = Field(default=False)  # ANTHROPIC_API_KEY 있어야 동작
    # 네이버 개발자 뉴스검색 API (백필용)
    naver_client_id: str = Field(default="")
    naver_client_secret: str = Field(default="")
    # 뉴스는 투표가 아닌 modulator (weighted_score 에 가산 + critical 게이트)
    ensemble_use_news: bool = Field(default=True)
    ensemble_news_weight: float = Field(default=0.3)
    # 뉴스 veto 임계값: 이 이하면 기술적 BUY 신호 거부. 기본 -0.4
    ensemble_news_veto_threshold: float = Field(default=-0.4)
    # 강한 부정 기사 비율 (sentiment_score <= veto_threshold 인 기사 비율) ≥ 이 값이면 매수 veto
    ensemble_news_strong_neg_ratio: float = Field(default=0.10)

    # 추가매수 파라미터 (포지션 보유 중 강한 신호 시 소량 추가)
    add_buy_enabled: bool = Field(default=True)
    add_buy_threshold: float = Field(default=0.60)       # 신규매수(0.40)보다 높게
    add_buy_min_votes: int = Field(default=3)            # 신규매수(2)보다 엄격
    add_buy_max_count: int = Field(default=1)            # 하루 최대 추가매수 횟수
    add_buy_fraction: float = Field(default=0.2)         # 계좌 20% (기본 40%의 절반)
    add_buy_max_position_pct: float = Field(default=0.70) # 계좌 70% 이상이면 추가매수 거부

    # DailyContext (5번째 앙상블 전략: 1일 이상 보유 포지션 차익실현) 파라미터
    daily_context_profit_gate_pct: float = Field(default=1.5)   # 게이트: 수익 최소 %
    daily_context_avwap_pct: float = Field(default=1.5)         # 플로팅: 세션VWAP 대비 %
    daily_context_pdh_pct: float = Field(default=1.0)           # 플로팅: 전일고가 대비 %
    daily_context_pdc_pct: float = Field(default=1.5)           # 플로팅: 전일종가 대비 %

    # Claude API 예산 (0이면 표시 안 함)
    api_budget_usd: float = Field(default=0.0)

    # 성과 측정 기준 초기 자금 (0이면 수익률% 미표시)
    initial_capital_krw: float = Field(default=0.0)
    # 성과 계산 시작일 (YYYY-MM-DD, 빈 문자열이면 전체)
    perf_start_date: str = Field(default="")
    # 거래 수수료율 (실현손익 차감용)
    trade_fee_buy_pct: float = Field(default=0.00015)   # 매수: 0.015%
    trade_fee_sell_pct: float = Field(default=0.00195)  # 매도: 0.015% + 증권거래세 0.18%

    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.trade_symbols.split(",") if s.strip()]

    @property
    def ensemble_weights_tuple(self) -> tuple[float, ...]:
        parts = [float(x) for x in self.ensemble_weights.split(",")]
        if len(parts) not in (4, 5):
            raise ValueError("ENSEMBLE_WEIGHTS must have 4 or 5 comma-separated floats")
        return tuple(parts)

    @property
    def kis_base_url(self) -> str:
        if self.kis_env == "paper":
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def is_paper(self) -> bool:
        return self.kis_env == "paper"


settings = Settings()
