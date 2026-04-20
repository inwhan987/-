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
    # 전략 선택: "ma_cross" | "rsi"
    trade_strategy: Literal["ma_cross", "rsi"] = Field(default="ma_cross")

    # RSI 파라미터
    trade_rsi_period: int = Field(default=14)
    trade_rsi_oversold: float = Field(default=30.0)
    trade_rsi_overbought: float = Field(default=70.0)

    # 실시간 러너 주기 (분)
    live_interval_minutes: int = Field(default=15)

    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.trade_symbols.split(",") if s.strip()]

    @property
    def kis_base_url(self) -> str:
        if self.kis_env == "paper":
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"

    @property
    def is_paper(self) -> bool:
        return self.kis_env == "paper"


settings = Settings()
