"""KRX 로그인 후 접근 가능한 데이터 확인 스크립트.

사용 전:
  환경변수 KRX_ID, KRX_PW 설정 후 실행
  python test_krx_login.py
"""
from pykrx import stock
import pandas as pd

DATE = '20260528'   # 테스트 날짜 (과거 거래일)

print('=' * 60)
print('KRX 로그인 데이터 접근 테스트')
print('=' * 60)

# ── 1) 종목 티커 목록 ───────────────────────────────────────────
print('\n[1] 종목 티커 목록')
for mkt in ['KOSPI', 'KOSDAQ']:
    try:
        tickers = stock.get_market_ticker_list(DATE, market=mkt)
        print(f'  {mkt}: {len(tickers)}개  예시: {tickers[:5]}')
    except Exception as e:
        print(f'  {mkt}: 실패 — {e}')

# ── 2) 전종목 일봉 (시/고/저/종/거래량/거래대금) ────────────────
print('\n[2] 전종목 일봉 OHLCV')
for mkt in ['KOSPI', 'KOSDAQ']:
    try:
        df = stock.get_market_ohlcv(DATE, market=mkt)
        print(f'  {mkt}: {len(df)}종목')
        if not df.empty:
            print(f'  컬럼: {list(df.columns)}')
            print(f'  상위 3행:\n{df.head(3)}')
    except Exception as e:
        print(f'  {mkt}: 실패 — {e}')

# ── 3) 거래대금 상위 랭킹 ────────────────────────────────────
print('\n[3] 거래대금 상위 (코스피+코스닥 통합)')
try:
    kospi = stock.get_market_ohlcv(DATE, market='KOSPI')
    kosdaq = stock.get_market_ohlcv(DATE, market='KOSDAQ')
    all_df = pd.concat([kospi, kosdaq])
    # 거래대금 컬럼 찾기
    val_col = [c for c in all_df.columns if '거래대금' in str(c) or 'value' in str(c).lower()]
    if val_col:
        top = all_df.nlargest(20, val_col[0])[[val_col[0]]]
        print(f'  거래대금 상위 20종목 (컬럼: {val_col[0]}):')
        print(top)
    else:
        print(f'  거래대금 컬럼 없음. 컬럼목록: {list(all_df.columns)}')
except Exception as e:
    print(f'  실패 — {e}')

# ── 4) 분봉 데이터 (5분봉) ────────────────────────────────────
print('\n[4] 5분봉 데이터 (삼성전자 1종목 테스트)')
try:
    df = stock.get_market_ohlcv(DATE, DATE, '005930', freq='m')  # 분봉
    print(f'  분봉 행수: {len(df)}  컬럼: {list(df.columns)}')
    print(df.head())
except Exception as e:
    print(f'  분봉 실패 — {e}')

try:
    df = stock.get_market_ohlcv_by_date(DATE, DATE, '005930')
    print(f'  일봉 확인: {df}')
except Exception as e:
    print(f'  일봉 실패 — {e}')

print('\n' + '=' * 60)
print('완료. 위 결과로 선별 백테스트 가능 여부 판단합니다.')
