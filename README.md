# stock-bot

한국투자증권(KIS) OpenAPI 기반 주식 거래 자동화 봇.
이동평균 크로스(골든/데드) + 손절 규칙으로 매매하고, 백테스트/실전 모드를 모두 지원합니다.

> 반드시 **모의투자(paper)** 로 먼저 충분히 검증한 뒤 실전 계좌로 전환하세요.
> 이 코드는 교육/개인용 예시이며, 발생하는 손실에 대한 책임은 사용자 본인에게 있습니다.

## 구성

```
stock_bot/
├── config/      설정 (pydantic-settings)
├── broker/      KIS API 클라이언트
├── strategy/    이동평균 크로스 전략
├── backtest/    backtrader 백테스트
├── live/        APScheduler 기반 실시간 러너
├── storage/     SQLite 거래 로그
└── notify/      텔레그램 알림
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

### 테스트

```bash
pytest
```

## 전략 로직

`stock_bot/strategy/ma_cross.py`

1. 단기(5일) MA 가 장기(20일) MA 를 **상향 돌파** → 매수
2. 단기 MA 가 장기 MA 를 **하향 돌파** → 매도
3. 보유 중 평단 대비 **-5% 손실** 도달 → 손절

기간/손절률은 `.env` 로 조정:
```
TRADE_SHORT_MA=5
TRADE_LONG_MA=20
TRADE_STOP_LOSS_PCT=5.0
TRADE_CASH_PER_TRADE=500000
TRADE_SYMBOLS=005930,000660
```

## 로드맵

- [ ] WebSocket 실시간 체결가 사용
- [ ] 분봉 기반 전략 추가
- [ ] Grafana 대시보드
- [ ] Docker 컨테이너화
- [ ] RSI / MACD 등 추가 지표
