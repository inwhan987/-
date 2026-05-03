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
python main.py backtest 005930.KS        # 5분봉 60일
python main.py backtest 005930.KS 30d 1d # 일봉 30일
```

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
| Supertrend | 24% | 추세 방향 |
| RSI | 16% | 과매수/과매도 |
| Bollinger | 12% | 밴드 이탈 후 회귀 |
| DailyContext | 20% | 오버나이트 포지션 청산 게이트 |

**매수 조건** (엄격): `score >= 0.40` AND BUY 표 ≥ 2  
**매도 조건** (빠르게): `score <= -0.30` AND SELL 표 ≥ 2  

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

### 오버나이트 청산 (DailyContext)

보유일수 ≥ 1일 AND 수익 ≥ 1.5% 조건을 만족하면 오버나이트 동적 매도를 허용합니다.

```
OVERNIGHT_SELL_THRESHOLD=-0.20
OVERNIGHT_MIN_SELL_VOTES=1
```

### 지지/저항 필터 (S/R)

일봉 60일치 swing high/low 기준으로 저항선 근처에서 매수를 억제합니다.

```
SR_ENABLED=true
SR_PROXIMITY_PCT=0.01   # 1% 이내 = 지지/저항 근처
SR_LOOKBACK_DAYS=60
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
- [x] 지지/저항 필터 (S/R)
- [x] 웹 대시보드 (FastAPI + SSE 실시간 로그)
- [x] Prometheus + Grafana 모니터링
- [x] 일별 자동 백업 (CSV → GitHub)
- [x] .env 핫리로드 (재시작 없이 파라미터 변경)
- [ ] 실시간 틱 기반 초단타 전략
