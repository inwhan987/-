# stock-bot

한국투자증권(KIS) OpenAPI 기반 주식 자동매매 봇.  
앙상블 전략(VWAP · Supertrend · RSI · 볼린저 · DailyContext) + 뉴스 감성 분석으로 매매하고,  
라즈베리파이에서 24시간 Docker로 운용합니다.

> 반드시 **모의투자(paper)** 로 먼저 충분히 검증한 뒤 실전 계좌로 전환하세요.  
> 이 코드는 개인용 예시이며, 발생하는 손실에 대한 책임은 사용자 본인에게 있습니다.

---

## 구성

```
stock_bot/
├── config/      설정 (pydantic-settings + .env 핫리로드)
├── broker/      KIS REST / WebSocket 클라이언트
├── strategy/    vwap / supertrend / rsi / bollinger / daily_context / ensemble
├── indicators/  ATR 등 보조지표
├── sizing.py    포지션 사이징 (fixed / fraction / atr)
├── news/        네이버 금융 크롤 + 감성 분석 (키워드 / Claude LLM)
├── backtest/    백테스트 엔진
├── live/        APScheduler 기반 실시간 러너 + 일별 백업
├── web/         FastAPI 대시보드 (SSE 실시간 로그)
├── storage/     SQLite 거래 로그 (trades.db)
└── notify/      디스코드 알림 + Prometheus 지표
```

---

## 셋업

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env 에서 KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO / DISCORD_WEBHOOK_URL 채우기
```

KIS 앱키 발급: https://apiportal.koreainvestment.com

---

## Docker로 실행 (권장)

```bash
docker compose up -d --build
docker compose logs -f
```

- `stock-bot` : 실매매 러너 (포트 9100 Prometheus)
- `stock-web`  : 웹 대시보드 → http://localhost:8001
- `prometheus` → http://localhost:9090
- `grafana`    → http://localhost:3000 (admin / admin)

> 로그 파일 실시간 확인 (컨테이너 재시작 후에도 끊기지 않음):
> ```bash
> tail -f ./logs/stock_bot.log
> ```

### 자동 업데이트 (Pi 권장)

`update.sh` 를 cron으로 등록하면 GitHub push 후 자동으로 반영됩니다.

```bash
# crontab -e
*/5 * * * * /home/inwhan/stock-bot/update.sh >> /home/inwhan/stock-bot/gitpull.log 2>&1
```

- `requirements.txt` / `Dockerfile` 변경 → 자동 `--build`
- 코드/설정 변경 → 재시작만 (빠름)

---

## 사용법

### 현재가 조회
```bash
python main.py quote 005930
```

### 수동 주문 (실제 주문 또는 dry-run)
```bash
python main.py order buy 005930 1 "수동 테스트"
python main.py order sell 005930 1 "목표가 도달"
```

### 뉴스 수동 크롤
```bash
python main.py news 005930
```

### 백테스트
```bash
python main.py backtest 005930.KS        # 5분봉 60일 (ensemble_dc 포함 전략 비교)
python main.py backtest 005930.KS 30d 1d # 일봉 30일
```

DailyContext가 포함된 `ensemble_dc` 전략도 기본 비교 목록에 포함됩니다.

### 시나리오 테스트
```bash
python tests/scenario_dc.py
```
223,000 매수 → 228,000 고점 → 하락 시나리오에서 DailyContext + Supertrend 신호가 어떻게 반응하는지 확인합니다.

### 웹 대시보드
```bash
python main.py web
# http://localhost:8001
```

---

## 전략: 앙상블

`TRADE_STRATEGY=ensemble` (기본값)

5개 서브전략의 가중 투표로 최종 시그널을 결정합니다.

| 서브전략 | 기본 가중치 | 역할 |
|---------|-----------|------|
| VWAP | 28% | 세션 VWAP 대비 이탈 방향 |
| Supertrend | 24% | 추세 방향 (p=7, m=2.5) |
| RSI | 16% | 과매수/과매도 |
| Bollinger | 12% | 밴드 근처 꺾임 감지 + 이탈 후 회귀 |
| DailyContext | 20% | 1일 이상 보유 포지션 차익실현 게이트 |

**매수 조건** (엄격): `score >= 0.40` AND BUY 표 ≥ 2  
**매도 조건** (빠르게): `score <= -0.30` AND SELL 표 ≥ 2  

### Supertrend 파라미터

25가지 조합(period × multiplier) 백테스트로 최적값 선정 (삼성전자 60일 5분봉 기준):

| 파라미터 | 수익률 | 거래 | 승률 | 샤프 | 비고 |
|---------|-------|------|------|------|------|
| **p=7, m=2.5** ★ | **+47.88%** | 10회 | 50.0% | **4.05** | 현재 설정 |
| p=10, m=2.5 | +43.06% | 11회 | 54.5% | 3.90 | |
| p=7, m=3.0 | +29.16% | 10회 | 40.0% | 2.84 | 구 설정 |

**m=2.5 vs m=3.0 실데이터 비교 (2026-04-30)**

| 설정 | 하락 인식 시간 | 차이 |
|-----|-------------|------|
| m=2.5 | 09:50 KST | — |
| m=3.0 | 11:30 KST | **100분 느림** |

ATR × multiplier 값이 클수록 밴드가 넓어져 하락 전환 인식이 느려집니다.  
`TRADE_SUPERTREND_PERIOD=7` / `TRADE_SUPERTREND_MULT=2.5`

### Bollinger 꺾임 감지

기존 조건(밴드를 실제로 돌파해야 신호)은 현실에서 거의 발생하지 않아 항상 HOLD였습니다.  
꺾임 감지를 추가해 밴드 **근처**에서 반전을 조기에 잡습니다.

```
band_pct = (종가 - 하단밴드) / (상단밴드 - 하단밴드)   → 0=하단, 1.0=상단
```

| 신호 | 조건 |
|-----|------|
| BUY  | band_pct ≤ 0.20 (하단 20% 이내) + 2봉 연속 상승 |
| SELL | band_pct ≥ 0.80 (상단 20% 이내) + 2봉 연속 하락 |

기존 조건(밴드 실제 돌파 후 회귀)도 유지됩니다.  
앙상블 12% 가중치로 단독 매매 불가 — 다른 전략과 합산 score가 임계값을 넘어야 발동합니다.

### 파라미터 튜닝 도구

```bash
# Supertrend 25가지 조합 백테스트
python tests/supertrend_tune.py [symbol] [period] [interval]

# Supertrend m=2.5 vs m=3.0 신호 비교 (특정 날짜)
python tests/supertrend_compare.py 2026-04-30
```

### 추가매수 (포지션 보유 중)

포지션이 있을 때 강한 신호 시 소량 추가매수합니다.

```
ADD_BUY_ENABLED=true
ADD_BUY_THRESHOLD=0.45       # 신규매수(0.40)보다 높게
ADD_BUY_MIN_VOTES=2
ADD_BUY_MAX_COUNT=2          # 하루 최대 2회
ADD_BUY_FRACTION=0.2         # 계좌 20%
ADD_BUY_MAX_POSITION_PCT=0.8 # 계좌 80% 이상이면 거부
```

### 장기보유 포지션 청산 (DailyContext)

앙상블의 나머지 4개 전략(VWAP·Supertrend·RSI·Bollinger)은 당일 장중 신호만 보기 때문에,  
전날 이전에 매수한 포지션에 대한 청산 판단을 제대로 하지 못합니다.  
DailyContext는 이 공백을 채워, **1일 이상 보유한 포지션**에 한해 차익실현 매도를 판단합니다.  
BUY 신호는 없고 SELL / HOLD 만 출력합니다.

**판단 흐름 (순서대로 모두 통과해야 SELL)**

| 단계 | 조건 | 기본값 |
|------|------|--------|
| Gate 1 | 보유일수 ≥ 1일 (당일 진입 포지션 제외) | — |
| Gate 2 | 평단 대비 수익 ≥ profit_gate_pct | 1.5% |
| Floating | 아래 3개 중 **1개 이상** 충족 | — |

**Floating 조건 (하나만 충족해도 됨)**

1. 세션 VWAP 대비 현재가 ≥ +1.5%
2. 전일 고가 대비 현재가 ≥ +1.0%
3. 전일 종가 대비 현재가 ≥ +1.5%

예시: 어제 매수 → 오늘 수익 2% → 세션 VWAP보다 1.5% 위에 있으면 SELL 투표

**매도 발동 조건 (DailyContext + Supertrend 동시 확인)**

Gate 1·2를 통과하고 Floating 조건도 충족한 상태에서,  
**DailyContext SELL + Supertrend 하락전환(SELL) 이 동시에 일치할 때만** 완화된 임계값으로 매도합니다.  
Supertrend가 아직 상승추세 유지(HOLD) 중이면 기본 임계값을 유지해 섣부른 청산을 방지합니다.

| 상황 | 적용 임계값 |
|------|------------|
| DailyContext SELL + Supertrend 하락전환 | 완화 (score ≤ -0.20, 1표) |
| DailyContext SELL + Supertrend 상승추세 유지 | 기본 (score ≤ -0.30, 2표) |

```
OVERNIGHT_SELL_THRESHOLD=-0.20
OVERNIGHT_MIN_SELL_VOTES=1
```

---

## 뉴스 크롤링 + 감성 분석

네이버 금융 종목 뉴스를 주기적으로 수집해 감성 점수(-1~+1)를 매기고 앙상블 시그널에 반영합니다.

### 크롤 스케줄

| 시간대 | 주기 |
|--------|------|
| 평일 장중 (09:00~15:00) | 5분마다 |
| 평일 장외 / 공휴일 | 1시간마다 |
| 주말 | 1시간마다 |

### Early Stop 최적화

DB에 저장된 최신 기사 시각 기준으로, 그보다 오래된 기사가 나오면 크롤을 즉시 중단합니다.  
(네이버 뉴스는 최신순 정렬 → 대부분 1~2페이지에서 종료)

### 주요 설정

```
NEWS_ENABLED=true
NEWS_PAGES_PER_SYMBOL=2      # 종목당 크롤 페이지 수
NEWS_LOOKBACK_HOURS=24
NEWS_PREFER_LLM=true         # Claude Haiku로 정확한 감성 분석
ENSEMBLE_NEWS_VETO_THRESHOLD=-0.6   # 이 이하면 기술적 BUY 신호 거부
```

### Claude LLM 감성 분석

키워드 방식보다 정확한 판단이 필요할 때:

```
NEWS_PREFER_LLM=true
ANTHROPIC_API_KEY=sk-ant-...
API_BUDGET_USD=4.16          # 잔여 크레딧 관리
```

신규 기사만 LLM 배치 호출 (중복 기사는 건너뜀).  
데이터는 `news.db` (SQLite)에 저장됩니다.

---

## 포지션 사이징

| 모드 | 수량 계산 | 설정 |
|------|-----------|------|
| `fixed` | `TRADE_CASH_PER_TRADE / 현재가` | 단순 고정 |
| `fraction` | `계좌 × POSITION_FRACTION / 현재가` | 복리 운용 |
| `atr` | `(계좌 × RISK_PCT) / (ATR × 배수)` | 변동성 기반 |

```
POSITION_SIZING=fraction
POSITION_FRACTION=0.4    # 계좌의 40%
```

---

## .env.overrides

시크릿을 제외한 튜닝 값은 `.env.overrides`에 작성하고 GitHub에 커밋합니다.  
Pi에서 `git pull` 후 `docker compose restart`만 하면 반영됩니다.

```
# .env.overrides 예시
LIVE_CANDLE=minute
LIVE_INTERVAL_MINUTES=5
ENSEMBLE_WEIGHTS=0.28,0.24,0.16,0.12,0.20
ENSEMBLE_BUY_THRESHOLD=0.4
ENSEMBLE_SELL_THRESHOLD=-0.3
```

`.env`의 시크릿(API 키 등)은 절대 커밋하지 마세요.

---

## 일별 백업

매일 00:05 KST에 자동으로 실행됩니다.

1. `trades.db` → `data/trades.csv`
2. `reviews.db` → `data/reviews.csv`
3. 전일 뉴스 → `data/news/YYYY-MM-DD.csv`
4. git push → GitHub

완료 시 디스코드 알림:
```
💾 일별 백업 완료 (2026-05-03)
체결 8건 · 리뷰 8건 · 뉴스(2026-05-02) 25건 → GitHub 업로드
```

수동 실행:
```bash
docker exec -e PYTHONPATH=/app -w /app/db stock-bot python -c "
import sys; sys.path.insert(0, '/app')
from stock_bot.live.backup import run_backup
run_backup()
"
```

---

## 모니터링

```
METRICS_PORT=9100
```

주요 Prometheus 지표:
- `stock_bot_last_price{symbol}`
- `stock_bot_position_qty{symbol}`
- `stock_bot_orders_total{symbol, side, mode}`
- `stock_bot_tick_errors_total{symbol}`

---

## 로드맵

- [x] Docker 컨테이너화 + 자동 업데이트 (update.sh)
- [x] 앙상블 전략 (VWAP · Supertrend · RSI · Bollinger · DailyContext)
- [x] 뉴스 크롤링 + 감성 분석 (키워드 / Claude LLM)
- [x] 뉴스 Early Stop 최적화
- [x] 포지션 사이징 (fixed / fraction / atr)
- [x] 추가매수 (포지션 보유 중 강한 신호)
- [x] 웹 대시보드 (FastAPI + SSE 실시간 로그)
- [x] Prometheus + Grafana 모니터링
- [x] 일별 자동 백업 (CSV → GitHub)
- [x] .env 핫리로드 (재시작 없이 파라미터 변경)
- [x] Supertrend 파라미터 튜닝 (25가지 조합 백테스트 → p=7, m=2.5 최적)
- [x] Bollinger 꺾임 감지 (밴드 근처 2봉 연속 반전 신호 추가)
- [ ] 실시간 틱 기반 초단타 전략
