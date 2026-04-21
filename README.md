# stock-bot

한국투자증권(KIS) OpenAPI 기반 주식 거래 자동화 봇.
이동평균 크로스(골든/데드) + 손절 규칙으로 매매하고, 백테스트/실전 모드를 모두 지원합니다.

> 반드시 **모의투자(paper)** 로 먼저 충분히 검증한 뒤 실전 계좌로 전환하세요.
> 이 코드는 교육/개인용 예시이며, 발생하는 손실에 대한 책임은 사용자 본인에게 있습니다.

## 구성

```
stock_bot/
├── config/      설정 (pydantic-settings)
├── broker/      KIS REST / WebSocket 클라이언트
├── strategy/    ma_cross / rsi / macd / bollinger / ensemble / news
├── indicators/  ATR 등 보조지표
├── sizing.py    포지션 사이징 (fixed / fraction / atr)
├── news/        네이버 금융 크롤 + 감성 분석
├── backtest/    backtrader 백테스트
├── live/        APScheduler 기반 실시간 러너
├── web/         FastAPI 대시보드
├── storage/     SQLite 거래 로그
└── notify/      텔레그램 알림 + Prometheus 지표
```

## 셋업

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env 열어서 KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO 채우기
```

KIS 앱키 발급: https://apiportal.koreainvestment.com

## 사용법

### 1. 백테스트 (먼저 전략 검증)

```bash
python main.py backtest 005930.KS   # 삼성전자
python main.py backtest AAPL        # 해외주식도 가능 (yfinance)
```

출력 예시:
```
              symbol: 005930.KS
          start_cash: 10000000
            end_cash: 11234000
          return_pct: 12.34
              sharpe: 0.82
     max_drawdown_pct: 8.5
```

### 2. 현재가 조회 (API 연결 확인)

```bash
python main.py quote 005930
```

### 3. 실전(모의투자) 러너

```bash
python main.py live
```

`.env` 의 `KIS_ENV=paper` 로 모의투자 환경에서 실행됩니다.
평일 09:00~15:30 KST 에만 동작하며, 기본 15분 주기로 시그널을 계산합니다.

> ⚠️ `TRADE_DRY_RUN=true` 가 기본값입니다. 이 상태에서는 시그널·주문 의도만 로그/텔레그램에 남고 **실제 주문은 전송되지 않습니다.**
> 충분히 검증한 뒤에만 `false` 로 바꾸세요.

### 4. Docker 로 상시 실행

```bash
docker compose up -d --build
docker compose logs -f
```

컨테이너는 `Asia/Seoul` 타임존으로 구동되며 `restart: unless-stopped` 로 자동 재시작됩니다.

### 테스트

```bash
pytest
```

## 전략

`TRADE_STRATEGY` 로 선택합니다.

### ma_cross (`stock_bot/strategy/ma_cross.py`)
1. 단기(5일) MA 가 장기(20일) MA 를 **상향 돌파** → 매수
2. 단기 MA 가 장기 MA 를 **하향 돌파** → 매도
3. 보유 중 평단 대비 **-5% 손실** 도달 → 손절

### rsi (`stock_bot/strategy/rsi.py`)
1. RSI < 30 (과매도) → 매수
2. RSI > 70 (과매수) → 매도
3. 손절 규칙은 동일

### macd (`stock_bot/strategy/macd.py`)
MACD(=EMA12−EMA26) 라인이 시그널(EMA9)을 상향 돌파 → 매수, 하향 돌파 → 매도.

### bollinger (`stock_bot/strategy/bollinger.py`)
평균회귀 전략. 하단 밴드 이탈 후 재진입 → 매수, 상단 돌파 후 회귀 → 매도.

### ensemble (`stock_bot/strategy/ensemble.py`)
4개 전략(ma_cross, macd, rsi, bollinger)의 **투표 + 가중 점수 하이브리드**.

- 각 전략 시그널: BUY=+1, HOLD=0, SELL=-1
- `score = Σ(signal × weight)`
- **매수** (까다롭게): `score >= 0.6` **AND** BUY 표 ≥ 2
- **매도** (빠르게): 손절 **OR** (`score <= -0.4` **AND** SELL 표 ≥ 1)

`.env` 에서:
```
TRADE_STRATEGY=ensemble
ENSEMBLE_WEIGHTS=0.3,0.3,0.2,0.2   # ma, macd, rsi, bb 순
ENSEMBLE_BUY_THRESHOLD=0.6
ENSEMBLE_SELL_THRESHOLD=-0.4
ENSEMBLE_MIN_BUY_VOTES=2
ENSEMBLE_MIN_SELL_VOTES=1
```

## 뉴스 크롤링 + 감성 분석

네이버 금융 종목 뉴스를 주기적으로 수집해 키워드 기반 감성 점수(-1~+1)를 매기고,
매매 시그널로 활용합니다.

```
NEWS_ENABLED=true
NEWS_CRAWL_INTERVAL_MINUTES=30   # 30분마다 크롤
NEWS_PAGES_PER_SYMBOL=1          # 종목당 네이버 뉴스 페이지 수
NEWS_LOOKBACK_HOURS=24           # 시그널 계산에 쓸 최근 기사 범위
NEWS_MIN_ARTICLES=3              # 이 이상일 때만 의사결정에 반영
NEWS_BUY_THRESHOLD=0.3
NEWS_SELL_THRESHOLD=-0.3
```

### 뉴스만 단독 전략으로 쓰기
```
TRADE_STRATEGY=news
```

### 앙상블에 5번째 투표로 합류
```
TRADE_STRATEGY=ensemble
ENSEMBLE_USE_NEWS=true
ENSEMBLE_NEWS_WEIGHT=0.2
```

### Claude API 로 의미 분석 (선택)
키워드 방식은 빠르지만 단순해요. 더 정확한 판단을 원하면:
```
NEWS_PREFER_LLM=true
ANTHROPIC_API_KEY=sk-ant-...
```

### 수동 크롤 (테스트)
```bash
python main.py news 005930 000660
```
출력 예시:
```
005930: new=12/total=20 | recent_24h: score=+0.43 (15 articles)
```

데이터는 `news.db` (SQLite) 에 저장됩니다.

## 포지션 사이징 (ATR + 동적 손절)

주문 수량을 결정하는 방식을 3가지 중 고를 수 있습니다.

| 모드 | 수량 계산 | 언제 쓰나 |
|------|-----------|-----------|
| `fixed` | `TRADE_CASH_PER_TRADE / 현재가` | 가장 단순. 복리 효과 없음 |
| `fraction` | `계좌평가금액 × POSITION_FRACTION / 현재가` | 복리로 불리고 싶을 때 |
| `atr` | `(계좌 × RISK_PER_TRADE_PCT%) / (ATR × ATR_STOP_MULTIPLIER)` | 변동성이 다른 종목을 섞어 거래할 때 추천 |

`atr` 모드는 추가로 손절률을 **ATR 기반 동적 값**으로 계산해 전략에 주입합니다.
변동성이 큰 날엔 손절선이 멀어지고, 조용한 날엔 빠듯해지는 효과.

```
POSITION_SIZING=atr
RISK_PER_TRADE_PCT=1.0        # 한 트레이드에 계좌의 1% 만 리스크
ATR_PERIOD=14
ATR_STOP_MULTIPLIER=2.0        # 손절거리 = ATR × 2
MAX_POSITION_PCT=30.0          # 한 종목 비중 상한
ACCOUNT_SIZE_KRW=10000000      # 0 이면 브로커에서 평가금액 자동 조회
```

## 웹 대시보드

```bash
python main.py web
# http://localhost:8000
```

보여주는 것:
- 설정 배너 (DRY-RUN / 전략 / 사이징 / 환경)
- 현재 포지션 (KIS 잔고 실시간 조회, 인증 없으면 빈 표)
- 최근 거래 (SQLite `trades.db`)
- 종목별 24h 감성 점수 + 뉴스 목록 (색상 코딩)
- JSON API: `/api/trades`, `/api/news`, `/healthz`

포트 변경: `WEB_PORT=8080`.

## 분봉 / 실시간 스트림

일봉 대신 분봉으로 매매하려면:
```
LIVE_CANDLE=minute
LIVE_MINUTE_INTERVAL=5     # 1/5/10/30/60
LIVE_INTERVAL_MINUTES=5    # 러너 주기도 같이 조정
```

실시간 체결가(WebSocket) 를 그냥 보고 싶으면:
```bash
python main.py stream 005930 000660
```

## 모니터링 (Prometheus + Grafana)

`.env` 에 `METRICS_PORT=9100` 을 설정하고 통합 스택을 띄우면 대시보드가 자동 프로비저닝됩니다.

```bash
docker compose up -d --build
# Grafana: http://localhost:3000 (admin / admin)
# Prometheus: http://localhost:9090
```

수집되는 주요 지표:
- `stock_bot_last_price{symbol}` — 최근 종가
- `stock_bot_position_qty{symbol}`, `stock_bot_position_avg_price{symbol}`
- `stock_bot_orders_total{symbol, side, mode}` — 주문 카운터
- `stock_bot_tick_errors_total{symbol}`

기간/손절률은 `.env` 로 조정:
```
TRADE_SHORT_MA=5
TRADE_LONG_MA=20
TRADE_STOP_LOSS_PCT=5.0
TRADE_CASH_PER_TRADE=500000
TRADE_SYMBOLS=005930,000660
```

## 로드맵

- [x] Docker 컨테이너화
- [x] RSI / MACD / 볼린저 밴드
- [x] Dry-run 모드
- [x] WebSocket 실시간 체결가
- [x] 분봉 기반 전략
- [x] Prometheus + Grafana 대시보드
- [x] 뉴스 크롤링 + 감성 분석
- [x] 포지션 사이징 고도화 (ATR 기반 + 동적 손절)
- [x] 웹 UI (FastAPI + Tailwind 대시보드)
- [ ] 실시간 틱 기반 초단타 전략
