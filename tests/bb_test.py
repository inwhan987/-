"""새 Bollinger 꺾임 감지 조건 - 4월 30일 시뮬레이션."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance as yf
import pandas as pd
from stock_bot.strategy.bollinger import decide_bollinger

df = yf.download('005930.KS', period='60d', interval='5m', auto_adjust=True, progress=False)
df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower() for c in df.columns]
df = df.dropna(subset=['close']).copy()
df.index = pd.to_datetime(df.index).tz_convert('Asia/Seoul')

day = df[df.index.date == pd.Timestamp('2026-04-30').date()]
full_idx = df.index.tolist()

print("4월 30일  새 Bollinger 꺾임 감지 시뮬 (매수가 223,000 가정)")
print("=" * 60)
print(f"{'시간':>6}  {'종가':>8}  {'신호':>5}  이유")
print("-" * 60)

found = False
for ts, row in day.iterrows():
    pos_in_full = full_idx.index(ts)
    if pos_in_full < 25:
        continue
    closes = df['close'].iloc[pos_in_full - 22: pos_in_full + 1]
    d = decide_bollinger(closes, window=20, k=2.0, position_qty=1, avg_price=223000)
    if d.signal.value != 'hold':
        t = ts.strftime("%H:%M")
        print(f"{t:>6}  {row['close']:>8,.0f}  {d.signal.value.upper():>5}  {d.reason}")
        found = True

if not found:
    print("  신호 없음")

print("=" * 60)

# 고점 근처 band_pct 확인
print("\n고점 구간 band_pct (상단근접도) 확인:")
print(f"{'시간':>6}  {'종가':>8}  {'band_pct':>9}")
print("-" * 35)
from stock_bot.strategy.bollinger import _bands, _band_pct
closes_all = df['close']
lower_s, _, upper_s = _bands(closes_all, 20, 2.0)

for ts, row in day.iterrows():
    pos_in_full = full_idx.index(ts)
    if pos_in_full < 22:
        continue
    c = float(row['close'])
    lo = float(lower_s.iloc[pos_in_full])
    up = float(upper_s.iloc[pos_in_full])
    bp = _band_pct(c, lo, up)
    if bp >= 0.70 or ts.strftime("%H:%M") in ["09:00","09:05","09:10","09:15","09:20","09:25","09:30"]:
        t = ts.strftime("%H:%M")
        print(f"{t:>6}  {c:>8,.0f}  {bp:>9.3f}  {'← 상단근처' if bp >= 0.85 else ''}")
