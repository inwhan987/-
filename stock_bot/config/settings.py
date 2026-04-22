"""Environment-driven configuration."""
from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    kis_app_key: str = Field(default="")
    kis_app_secret: str = Field(default="")
    kis_account_no: str = Field(default="")
    kis_env: Literal["paper", "real"] = Field(default="paper")

    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    trade_symbols: str = Field(default="005930")
    trade_cash_per_trade: int = Field(default=500_000)
    trade_stop_loss_pct: float = Field(default=5.0)
    trade_short_ma: int = Field(default=5)
    trade_long_ma: int = Field(default=20)

    # 주문을 실제로 내지 않고 로그만 남김 (실전 전환 전 필수 검증 단계)
    trade_dry_run: bool = Field(default=True)
    # 전략 선택
    trade_strategy: Literal[
        "ma_cross", "rsi", "macd", "bollinger", "ensemble", "news"
    ] = Field(default="ma_cross")

    # 앙상블 파라미터
    ensemble_weights: str = Field(default="0.3,0.3,0.2,0.2")  # ma,macd,rsi,bb
    ensemble_buy_threshold: float = Field(default=0.6)
    ensemble_sell_threshold: float = Field(default=-0.4)
    ensemble_min_buy_votes: int = Field(default=2)
    ensemble_min_sell_votes: int = Field(default=1)

    # RSI 파라미터
    trade_rsi_period: int = Field(default=14)
    trade_rsi_oversold: float = Field(default=30.0)
    trade_rsi_overbought: float = Field(default=70.0)

    # MACD 파라미터
    trade_macd_fast: int = Field(default=12)
    trade_macd_slow: int = Field(default=26)
    trade_macd_signal: int = Field(default=9)

    # Bollinger 파라미터
    trade_bb_window: int = Field(default=20)
    trade_bb_k: float = Field(default=2.0)

    # 포지션 사이징
    position_sizing: Literal["fixed", "fraction", "atr"] = Field(default="fixed")
    position_fraction: float = Field(default=0.02)  # 2% of account
    risk_per_trade_pct: float = Field(default=1.0)  # ATR 모드: 한 번에 계좌 1% 리스크
    atr_period: int = Field(default=14)
    atr_stop_multiplier: float = Field(default=2.0)
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
    news_crawl_interval_minutes: int = Field(default=30)
    news_pages_per_symbol: int = Field(default=1)
    news_lookback_hours: int = Field(default=24)
    news_min_articles: int = Field(default=3)
    news_buy_threshold: float = Field(default=0.3)
    news_sell_threshold: float = Field(default=-0.3)
    news_prefer_llm: bool = Field(default=False)  # ANTHROPIC_API_KEY 있어야 동작
    # 네이버 개발자 뉴스검색 API (백필용)
    naver_client_id: str = Field(default="")
    naver_client_secret: str = Field(default="")
    # 앙상블에서 뉴스를 5번째 구성요소로 합류시킬지
    ensemble_use_news: bool = Field(default=False)
    ensemble_news_weight: float = Field(default=0.2)

    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.trade_symbols.split(",") if s.strip()]

    @property
    def ensemble_weights_tuple(self) -> tuple[float, float, float, float]:
        parts = [float(x) for x in self.ensemble_weights.split(",")]
        if len(parts) != 4:
            raise ValueError("ENSEMBLE_WEIGHTS must have 4 comma-separated floats")
        return tuple(parts)  # type: ignore[return-value]

    @property
    def kis_base_url(self) -> str:
        if self.kis_env == "paper":
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def is_paper(self) -> bool:
        return self.kis_env == "paper"


settings = Settings()
