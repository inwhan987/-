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
    # 종목 시장지수(.KS→코스피, .KQ→코스닥) 일봉이 N일MA아래 & M일모멘텀− 면 신규 BUY 차단.
    # 매도/손절/익절은 정상 동작. MA·모멘텀 기간은 파라미터(기본 20MA·10일).
    regime_block_enabled: bool = Field(default=True)
    regime_ma_period: int = Field(default=20)   # 지수 레짐 이동평균 기간(일). naver_index 기본값과 일치
    regime_mom_days: int = Field(default=10)    # 지수 레짐 모멘텀 룩백(일)

    # 종목 일봉 게이트 (개별 종목 하락추세 시 신규 미진입)
    # 그 종목 *자신*의 일봉이 50MA아래 AND 50MA가 N일새 기울기% 이상 하락 시 신규 BUY 차단.
    # 지수 레짐(시장 전체)과 별개 — "시장은 멀쩡한데 이 종목만 가파르게 빠짐"을 잡는다.
    # 매도/손절/익절은 정상 동작. 일봉은 당일 1회 캐시(KIS 유량 보호).
    stock_daily_gate_enabled: bool = Field(default=False)
    stock_daily_gate_ma: int = Field(default=50)          # 일봉 MA 기간
    stock_daily_gate_slope_days: int = Field(default=5)   # 기울기 룩백(일)
    stock_daily_gate_slope_pct: float = Field(default=1.0) # MA가 룩백새 이만큼(%) 이상 하락 시 차단

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
    leader_entry_mode: str = Field(default="pullback")    # 진입 방식(2026-08-10 default OR로 전환): pullback=OR 모드(VWAP 먼저 시도→신호 있으면 vwap 사용, 없으면 스윙저점+RECLAIM 폴백) · vwap_touch=VWAP 단독(폴백 없음). OR 모드에서만 w·max_pull·phwin·fib·anchor·reclaim·volfilter 유효. tp·bar_range·close_time 동일.
    leader_vwap_tol: float = Field(default=0.3)            # vwap_touch 전용: VWAP 터치 허용오차 %. 이번봉 저가 ≤ VWAP×(1+tol%) 이면 '터치' 인정. 백테 최적 0.3(=0.3%). pullback 모드에선 무시. (기본 0.3)
    leader_max_pull_pct: float = Field(default=5.0)       # 전고점 대비 최대 눌림 %(=붕괴컷 floor, 06-22 스윕: 7→5 얕은눌림만)
    leader_phwin_min: int = Field(default=0)              # ① 전고점 윈도우: 0=9:00~기준시각(현행) · >0=(기준시각−N분)~기준시각 롤링. 롤링은 늦은 첫선별·섹터전환의 시간종속(stale 전고점) 완화 — 기준시각이 전환 시 전환시각으로 갱신돼 항상 '직전 N분'을 봄. (기본 0=끔)
    leader_fib_pct: float = Field(default=0.0)            # ⛔기각·미사용(백테스트: 고정%가 이김, 노이즈)·웹숨김. 필드는 코드가 읽어 유지. 피보 되돌림 floor(0=끔·고정%유지). >0 이면 leader_max_pull_pct 대체
    leader_anchor: str = Field(default="off")             # 동적 앵커: off/ema/vwap/both — 스윙저점이 앵커 위에서 형성돼야 유효(섹터전환 대비 시간종속 없는 기준). both=EMA·VWAP 컨플루언스(둘 중 높은 쪽)
    leader_anchor_ema: int = Field(default=20)            # ema 앵커 기간(봉수)
    leader_anchor_tol: float = Field(default=0.0)         # 앵커 허용오차 %: 스윙저점 < 앵커×(1-tol/100) 이면 컷
    leader_volfilter: float = Field(default=0.0)          # ⛔기각·미사용(백테스트: off 최선)·웹숨김. 필드는 코드가 읽어 유지. 경량 거래량 필터: 스윙저점봉 거래량 ≤ 배수×아침임펄스 평균거래량 이어야 유효 (0=끔)
    leader_fib_dynamic: bool = Field(default=False)       # ⛔기각·미사용(백테스트: 강도로 승패 못 가름)·웹숨김. 필드는 코드가 읽어 유지. 동적 피보(관측용): 아침 임펄스다리 상승강도로 되돌림 깊이 자동결정(강도<10%→고정pull, 10~15%→0.382, ≥15%→0.5) + 깊게푼 밴드에 EMA 가드. 켜면 leader_fib_pct 무시. (4일표본 백테스트로 강도는 승패 못 갈랐음 — 검증된 엣지 아님, 관전 관찰용)
    leader_reclaim: bool = Field(default=True)            # 회복확인: 확정봉 종가 > 직전봉 고가일 때만 진입
    leader_band_ratio: float = Field(default=0.6)         # 60%룰 통일: 종목 basket·섹터 시딩·섹터 재정렬 모두 1등 stock_score/sector_score 대비 N 이상만 편입
    leader_bar_range_pct: float = Field(default=1.5)      # 장대양봉컷: 진입 확정봉 (고-저)/저 > N% 면 진입 차단 (0=비활성)
    leader_daily_trend_gate: bool = Field(default=False)  # 🔒예약·미사용(관측 전용). 일봉추세 라벨은 leader_finder가 picks·로그에 항상 남기지만, 이 게이트는 아직 어떤 코드도 읽지 않아 선별/진입에 영향 0. '추세 나쁜 종목은 실제로 덜 오르나' 상관 확인 후 나중에 진입필터로 승격 예정. (기본 False)
    leader_close_time: str = Field(default="14:55")       # 강제 마감청산 시각
    # ── 대장주 선별 기준 (leader_finder 게이트 임계값 · 기본값=현행 하드코딩값) ──
    # leader_runner 가 leader_finder subprocess 에 CLI 인자로 주입한다. 선별 알고리즘
    # (4게이트·핫섹터·테마병합·정렬)은 그대로이고 임계값만 조절한다. 기본값은 현재
    # 운영값과 100% 동일 → 값을 안 바꾸면 동작 불변. 값을 바꾸면 선별 결과가 달라지는
    # 전략 변경이므로 백테스트 후 조정 권장.
    leader_sel_top: int = Field(default=100)              # 거래대금 상위 N(코스피·코스닥 각) → 통합 상위 2N
    leader_sel_rise_min: float = Field(default=5.0)       # 자격① 등락률 하한 %
    leader_sel_hot_min: int = Field(default=3)            # 자격 종목 N개↑ 섹터만 핫섹터로 인정
    leader_sel_vol_mult: float = Field(default=2.0)       # 자격④ 거래대금 평소(5일평균·세션보정)대비 배수 하한
    leader_sel_min_value_eok: float = Field(default=400.0)   # 자격② 거래대금 최소 절대값(억원). intraday_flow 배수로 장중 시각비례 자동 조정.
    leader_sel_dyn_value_pct: float = Field(default=0.0)     # (deprecated) §2 유니버스합 비율. 0=미적용(권장), >0이면 intraday_flow 무시하고 이 방식 우선.
    # ── intraday_flow: 시각비례 자동 배수 ──────────────────────────────
    # 오늘 이 시각까지 top-N 거래대금 합 / 과거 같은 시각 baseline. 활황이면 배수>1로
    # min_value 상향, 조용하면 배수<1로 완화. clamp로 극단값 방어.
    # 정밀모드(캐시 ≥3일)와 폴백모드(하루완결 5d avg × frac) 자동 전환.
    leader_mf_clamp_low:    float = Field(default=0.5)       # 배수 하한 (조용한 날 방어)
    leader_mf_clamp_high:   float = Field(default=2.0)       # 배수 상한 (활황기 방어)
    leader_sel_sector_top3: bool = Field(default=True)       # §4-1 섹터강도: 상위 3종목만으로 강도·균등도 계산(꼬리 제외+쏠림 페널티). 기본 ON.
    leader_sel_min_cap_eok: float = Field(default=1000.0)    # 자격③ 시가총액 최소(억원, 0이면 통과)
    leader_sel_max_change: float = Field(default=25.0)    # 과열컷: 등락률 상한 %(초과 종목은 대장주 후보 제외)
    # ── Level1 회전율 개선: 유통주식수 근사 분모 + 시간대 계단 게이트 + 극단치 캡 ──
    # 회전율 분모: 유통주식수(KIS lstn_stcn, Naver 는 market_cap/price 역산) — 시총 나눗셈보다
    # 소형주 자동 편향 완화. 시간대 계단: 요구회전율 = base + slope × 세션경과율(%)
    # → 09:30 최저 1%, 15:20 최저 16%. 항상 활성.
    leader_sel_turnover_gate_base:  float = Field(default=1.0)   # 게이트 base(%)
    leader_sel_turnover_gate_slope: float = Field(default=15.0)  # 게이트 slope(%)
    leader_sel_turnover_cap_pct:    float = Field(default=200.0) # pctile 입력값 캡(%), 0=무제한
    # ── 섹터 전환(관전 실험, 기본 off) ────────────────────────────────
    # 선별 로직(leader_finder 점수화)은 그대로. 장중 재선별(--reval)을 주기적으로
    # 돌려 '다른 섹터가 확실히 더 강해졌으면' 감시/매매 섹터를 갈아탄다.
    leader_switch_enabled: bool = Field(default=True)      # 섹터 전환 마스터 토글 (2026-08-08 기본 ON으로 승격)
    leader_switch_interval_min: int = Field(default=30)    # 재선별·전환 판정 주기(분)
    leader_switch_until: str = Field(default="13:00")      # 이 시각 이후엔 전환 중지
    leader_switch_watch_sectors: int = Field(default=3)    # 감시할 상위 섹터 수(각 1등, 차트+전환후보)
    leader_switch_hysteresis: int = Field(default=2)       # 새 섹터가 현 섹터보다 상승종목수 ≥N 앞설 때만 전환(근소차 무시)
    leader_switch_move_max_pct: float = Field(default=1.0) # 현 섹터 대장이 직전 판정 대비 >N% 오르면(작동중) 전환 보류
    leader_sector_switch_threshold: float = Field(default=0.10)  # 점수 기반 전환: 신섹터 점수 > 현섹터 × (1+N) 이어야 전환(기본 10%)
    leader_max_sectors: int = Field(default=3)             # 동시 감시 최대 섹터 수(섹터당 최대 3종목 = 최대 9종목)
    # ── 섹터 슬롯·60%룰 통일 (요구사항 §4·§5) ─────────────────────
    # 다중 섹터 슬롯·60%룰 항상 활성. 종목 basket·섹터 시딩·섹터 재정렬 모두 leader_band_ratio 사용.
    # 종목 점수 가중치: log거래대금·수급·상승률·회전율·급증배율 (합=1.0 권장) — B적극
    lead_st_w_value:    float = Field(default=0.30)
    lead_st_w_flow:     float = Field(default=0.25)
    lead_st_w_updn:     float = Field(default=0.30)
    lead_st_w_turnover: float = Field(default=0.08)
    lead_st_w_surge:    float = Field(default=0.07)
    # 수급 조회 실패 시 fallback 가중치 — 수급 항목 제거(0), B적극 비율로 재분배 (합=1.0)
    lead_st_nf_w_value:    float = Field(default=0.40)
    lead_st_nf_w_updn:     float = Field(default=0.40)
    lead_st_nf_w_turnover: float = Field(default=0.11)
    lead_st_nf_w_surge:    float = Field(default=0.09)
    # 섹터 점수 가중치: 강도(intensity)·균등도(breadth) — 합=1.0 권장
    # sector_score = mean(종목스코어) × (intensity + breadth × mean/max)
    lead_sc_w_intensity: float = Field(default=0.65)
    lead_sc_w_breadth:   float = Field(default=0.35)
    # 스크리너 min_pos_ratio 하락장 완화값 (기본 0.3 — 상승장: 0.5 고정)
    screener_sector_pos_ratio_down: float = Field(default=0.3)
    # own-symbol 우선권: ON 이면 대장주봇이 스톡봇 종목(symbols)도 매매 가능.
    # 단 종목 점유락(position_owner)으로 상호배제 — 스톡봇이 비운 종목만 대장주가 잡고,
    # 대장주가 잡은 동안 스톡봇은 그 종목 매수·매도·판단 전부 정지. OFF=기존 완전분리.
    leader_own_symbol_priority: bool = Field(default=False)

    # LLM 호출 백엔드: "api"(Anthropic SDK, 기본) | "claude_code"(파이 구독 CLI, 사용료 0).
    # 리뷰봇·장전검수·뉴스감성이 이 값을 읽어 백엔드를 고른다. 파라미터탭에서 저장 시
    # 핫리로드로 다음 호출부터 즉시 반영(재시작 불필요). 문제 시 api 로 되돌리면 롤백.
    llm_backend: str = Field(default="api")

    # LLM 모델 선택 (claude_code 백엔드에서 각 기능이 쓸 Claude 모델 별칭).
    # haiku|sonnet|opus|fable — 구독 해석: opus=Opus4.8, sonnet=Sonnet5, haiku=Haiku4.5, fable=Fable5.
    # 파라미터탭 저장 시 핫리로드로 다음 호출부터 반영. api 백엔드에선 각 모듈 기본 모델 사용.
    premarket_review_model: str = Field(default="sonnet")   # 장전 검수
    daily_review_model: str = Field(default="sonnet")       # 장마감 리뷰
    news_sentiment_model: str = Field(default="haiku")      # 뉴스 감성분석

    # Claude API 예산 (0이면 표시 안 함). A안: 이번 충전액만 입력.
    api_budget_usd: float = Field(default=0.0)
    # 마지막 충전(리셋) 시점 epoch UTC. 파라미터탭에서 예산 저장 시 자동 기록.
    # 잔여 = api_budget_usd − (이 시점 이후 누적 사용액). 0이면 전체 기간 집계.
    api_budget_reset_at: float = Field(default=0.0)

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
