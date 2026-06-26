"""Environment-driven configuration."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
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
    # KIS 초당 호출 유량 한도. 0=환경별 자동(공식: 모의 1건/초, 실전 18건/초).
    # 한도에 걸리기 전에 호출 간격을 띄우는 능동 RateLimiter 가 이 값을 사용한다.
    kis_rate_per_sec: float = Field(default=0.0)
    # 두 봇(stock-bot/leader-bot)이 같은 앱키를 공유하므로, 프로세스 간 유량
    # 조율용 공유 락 파일. 빈값=자동(컨테이너 공유 마운트 /app/data 가 있으면 사용,
    # 없으면 프로세스 내부 게이트만). 두 컨테이너가 같은 호스트 경로를 봐야 한다.
    kis_gate_file: str = Field(default="")

    discord_webhook_url: str = Field(default="")

    # SYMBOLS(.env.overrides 스크리너 자동갱신) 과 TRADE_SYMBOLS(.env 기본값) 둘 다 허용
    # 우선순위: .env.overrides(SYMBOLS) > .env(TRADE_SYMBOLS)
    trade_symbols: str = Field(
        default="005930",
        validation_alias=AliasChoices("SYMBOLS", "TRADE_SYMBOLS"),
    )
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

    # 앙상블 MACD 6번째 전략 (additive: 기존 가중치 그대로 + MACD 가중치 추가)
    ensemble_macd_enabled: bool = Field(default=False)
    ensemble_macd_weight: float = Field(default=0.225)
    ensemble_macd_fast: int = Field(default=12)
    ensemble_macd_slow: int = Field(default=26)
    ensemble_macd_signal: int = Field(default=9)

    # 앙상블 EMA 추세 방향 7번째 전략 (additive: EMA fast>slow 구간 내내 BUY)
    ensemble_ema_trend_enabled: bool = Field(default=False)
    ensemble_ema_trend_weight: float = Field(default=0.15)
    ensemble_ema_trend_fast: int = Field(default=9)
    ensemble_ema_trend_slow: int = Field(default=21)

    # 모멘텀(ROC) 파라미터
    trade_momentum_period: int = Field(default=10)
    trade_momentum_threshold: float = Field(default=0.0)

    # Bollinger 파라미터
    trade_bb_window: int = Field(default=20)
    trade_bb_k: float = Field(default=2.0)
    trade_bb_consec: int = Field(default=3)  # 꺾임 감지 연속 봉 수 (2 or 3)

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

    # 장마감 전 신규매수 차단 (예: 15:00~15:30 마감 30분 전부터 BUY 차단)
    # SELL/stop_loss 는 모두 통과.
    close_block_enabled: bool = Field(default=False)
    close_block_start: str = Field(default="15:00")

    # 분할 익절 (take-profit partial sell)
    # 보유 포지션 수익률이 take_profit_pct 이상이 되면 take_profit_fraction 만큼 부분 매도.
    # 하루 1회만 발동 (재진입 후 다시 발동 가능).
    take_profit_enabled: bool = Field(default=False)
    take_profit_pct: float = Field(default=5.0)       # 익절 발동 수익률 (%)
    take_profit_fraction: float = Field(default=0.30) # 매도 비율 (0.30 = 30%)

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

    # ── 실시간 러너: 아래 두 값은 의미가 다르니 혼동 주의 ──
    # (1) 러너 틱 실행 주기 = 몇 분마다 _tick() 을 도는가 (스케줄 간격)
    live_interval_minutes: int = Field(default=15)
    # 캔들 소스: "daily" | "minute"
    live_candle: Literal["daily", "minute"] = Field(default="daily")
    # (2) 캔들 N분봉 간격 = minute 모드에서 몇 분봉을 쓰는가 (1/3/5/10/15/30/60)
    #     ※ live_interval_minutes(틱 주기) 와 다른 값! env: LIVE_CANDLE_MINUTES
    live_candle_minutes: int = Field(default=5)

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
    news_relevance_filter: bool = Field(default=True)  # 제목에 종목명/코드 없는 기사 제외
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
    add_buy_require_trend_agree: bool = Field(default=True)  # ST 하락추세면 추가매수 차단
    add_buy_inherit_initial_stop: bool = Field(default=True) # 추가매수 시 초기 stop_pct 유지

    # 손절 후 재진입 쿨다운 (분 단위, 0이면 비활성)
    post_stoploss_cooldown_min: int = Field(default=30)

    # HTF 하락추세 매수 차단 (ADX 기반)
    htf_block_enabled: bool = Field(default=False)
    htf_block_tf_minutes: int = Field(default=30)       # 리샘플링 타임프레임 (분)
    htf_block_adx_period: int = Field(default=14)       # ADX 계산 기간
    htf_block_adx_threshold: float = Field(default=30.0) # ADX > 임계값 AND -DI > +DI 시 차단
    htf_ma_override_enabled: bool = Field(default=True)  # MA 근접 시 차단 해제
    htf_ma_override_span: int = Field(default=120)     # 5분봉 EMA 기간 (fallback 자동)
    htf_ma_override_pct: float = Field(default=1.5)    # MA 근접 임계값 (%)

    # 베어장 신규 미진입 게이트 (일봉 지수 레짐)
    # 종목 시장지수(.KS→코스피, .KQ→코스닥) 일봉이 50MA아래 & 10일모멘텀− 면 신규 BUY 차단.
    # 임계값(50MA·10일)은 naver_index.py 상수 고정. 매도/손절/익절은 정상 동작.
    regime_block_enabled: bool = Field(default=True)

    # DailyContext (5번째 앙상블 전략: 1일 이상 보유 포지션 차익실현) 파라미터
    daily_context_profit_gate_pct: float = Field(default=1.5)   # 게이트: 수익 최소 %
    daily_context_avwap_pct: float = Field(default=1.5)         # 플로팅: 세션VWAP 대비 %
    daily_context_pdh_pct: float = Field(default=1.0)           # 플로팅: 전일고가 대비 %
    daily_context_pdc_pct: float = Field(default=1.5)           # 플로팅: 전일종가 대비 %
    daily_context_trend_bonus: float = Field(default=0.5)       # ST 상승 시 PCT 가산값

    # 매도 타이밍: true = 다음 봉 시가 지연 체결 (default), false = 즉시 시장가
    # 백테스트와 라이브 동일 적용. 손절/긴급매도는 무조건 즉시.
    sell_on_next_open: bool = Field(default=True)

    # ── 대장주 눌림목 전략 (leader_trader) ──────────────────────────────
    # 9:30(재시도 시 그 시각) 선별 대장주 바스켓을 3분봉 감시, 스윙저점 확정 시
    # 하루 1종목 진입. 손절 = 스윙저점×(1-buf), 익절 +tp%, 14:55 마감청산.
    # 기존 앙상블 전략과 자본 분리: leader_budget_krw 고정 예산만 사용.
    leader_trade_enabled: bool = Field(default=False)
    leader_budget_krw: float = Field(default=1_000_000)   # 1회 진입 예산 (원)
    leader_interval_min: int = Field(default=3)           # 감시 봉 간격 (분)
    leader_w: int = Field(default=2)                      # 스윙저점 좌우 확인 봉수
    leader_stop_buf_pct: float = Field(default=1.5)       # 손절 = 스윙저점 -N%
    leader_tp_pct: float = Field(default=4.0)             # 익절 +N%
    leader_max_pull_pct: float = Field(default=5.0)       # 전고점 대비 최대 눌림 %(=붕괴컷 floor, 06-22 스윕: 7→5 얕은눌림만)
    leader_reclaim: bool = Field(default=True)            # 회복확인: 확정봉 종가 > 직전봉 고가일 때만 진입
    leader_top3_ratio: float = Field(default=0.6)         # 2·3등 바스켓 편입: 1등 등락률 대비 비율
    leader_bar_range_pct: float = Field(default=1.5)      # 장대양봉컷: 진입 확정봉 (고-저)/저 > N% 면 진입 차단 (0=비활성)
    leader_close_time: str = Field(default="14:55")       # 강제 마감청산 시각
    # own-symbol 우선권: ON 이면 대장주봇이 스톡봇 종목(symbols)도 매매 가능.
    # 단 종목 점유락(position_owner)으로 상호배제 — 스톡봇이 비운 종목만 대장주가 잡고,
    # 대장주가 잡은 동안 스톡봇은 그 종목 매수·매도·판단 전부 정지. OFF=기존 완전분리.
    leader_own_symbol_priority: bool = Field(default=False)

    # Claude API 예산 (0이면 표시 안 함)
    api_budget_usd: float = Field(default=0.0)

    # 성과 측정 기준 초기 자금 (0이면 수익률% 미표시). 전략별 원금의 합으로 운용.
    initial_capital_krw: float = Field(default=0.0)
    # 전략별 초기 자금(원금) — 각 전략 수익률%의 분모. 합 = initial_capital_krw.
    stock_capital_krw: float = Field(default=0.0)   # 스톡봇(앙상블) 운용 원금
    leader_capital_krw: float = Field(default=0.0)  # 대장주 눌림목 운용 원금
    # 성과 계산 시작일 (YYYY-MM-DD, 빈 문자열이면 전체)
    perf_start_date: str = Field(default="")
    # 거래 수수료율 (실현손익 차감용)
    trade_fee_buy_pct: float = Field(default=0.00015)   # 매수: 0.015%
    trade_fee_sell_pct: float = Field(default=0.00195)  # 매도: 0.015% + 증권거래세 0.18%

    @property
    def symbols(self) -> list[str]:
        result = []
        for s in self.trade_symbols.split(","):
            s = s.strip()
            if not s:
                continue
            # .KS / .KQ suffix 제거 → 6자리 코드로 통일
            s = s.split(".")[0]
            result.append(s)
        return result

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

    @property
    def kis_rate_limit(self) -> float:
        """초당 허용 호출 수. 0(자동)이면 공식 한도 모의 1 / 실전 18."""
        if self.kis_rate_per_sec and self.kis_rate_per_sec > 0:
            return self.kis_rate_per_sec
        return 1.0 if self.kis_env == "paper" else 18.0

    @property
    def kis_gate_path(self) -> str | None:
        """프로세스 간 유량 조율용 공유 락 파일 경로. 없으면 None(내부 게이트만)."""
        if self.kis_gate_file:
            return self.kis_gate_file
        import os
        # 두 봇 컨테이너가 공유 마운트하는 /app/data
        if os.path.isdir("/app/data"):
            return "/app/data/.kis_rate_gate"
        return None


settings = Settings()
