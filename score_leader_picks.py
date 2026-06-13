"""대장주 전진검증 점수화 — leader_finder가 라이브 10시에 저장한 선별결과를
다음날(또는 이후) 실제 그날 성과와 대조해 '진짜 대장주였는지' 평가한다.

선별은 라이브 전체시장(네이버 거래대금 상위100)에서 이뤄지므로 유니버스 한계가 없다.
점수화는 종목코드별 일봉(pykrx 단일종목 by_date — 이 환경에서 동작 확인됨)으로,
  · 당일 등락률(종가/전일종가)
  · 당일 고가 도달폭(고가/전일종가)
  · 선별가 대비 종가 수익(종가/선별시점가 − 1)
을 계산. 시장 전체에서 그날 상위 상승률과 비교해 '대장주 적중'을 가늠한다.

사용:
  python score_leader_picks.py                 # data/leader_picks/*.json 전부 채점
  python score_leader_picks.py 2026-06-01      # 특정 날짜만
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).parent
PICKS_DIR = HERE / "data" / "leader_picks"


def _day_ohlcv(code: str, date: str):
    """date(YYYY-MM-DD) 그 종목 일봉 1행 + 전일종가. 실패 None."""
    from pykrx import stock
    d = date.replace("-", "")
    start = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=12)).strftime("%Y%m%d")
    try:
        df = stock.get_market_ohlcv_by_date(start, d, code)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df.columns = ["open", "high", "low", "close", "volume", "chg"][:len(df.columns)]
    if d[:4] + "-" + d[4:6] + "-" + d[6:] not in [str(i.date()) for i in df.index]:
        # 그 날짜 행이 없으면(휴장/미반영) 마지막 행을 그날로 보지 않음
        if str(df.index[-1].date()) != date:
            return None
    row = df.iloc[-1]
    prev_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else None
    return {"open": float(row["open"]), "high": float(row["high"]),
            "low": float(row["low"]), "close": float(row["close"]),
            "prev_close": prev_close}


def score_file(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    date = data["date"]
    leaders = data["leaders"]
    print("=" * 92)
    print(f"■ {date} 선별({data['selected_at']}) 대장주 {len(leaders)}종목 — 당일 실제 성과")
    print(f"  파라미터 {data['params']}")
    print("-" * 92)
    print(f"{'종목':<14}{'코드':>8}{'선별등락':>9}{'당일종가등락':>12}"
          f"{'당일고가폭':>11}{'선별가대비종가':>14}")
    print("-" * 92)
    rows = []
    for L in leaders:
        o = _day_ohlcv(L["code"], date)
        if not o or not o["prev_close"]:
            print(f"{L['name'][:12]:<14}{L['code']:>8}{L['change_pct']:>+8.1f}%"
                  f"{'(일봉없음)':>12}")
            continue
        day_ret = (o["close"] / o["prev_close"] - 1) * 100
        high_ret = (o["high"] / o["prev_close"] - 1) * 100
        from_sel = (o["close"] / L["price"] - 1) * 100 if L["price"] else float("nan")
        rows.append((L, day_ret, high_ret, from_sel))
        print(f"{L['name'][:12]:<14}{L['code']:>8}{L['change_pct']:>+8.1f}%"
              f"{day_ret:>+11.1f}%{high_ret:>+10.1f}%{from_sel:>+13.1f}%")
    if rows:
        n = len(rows)
        avg_day = sum(r[1] for r in rows) / n
        avg_high = sum(r[2] for r in rows) / n
        avg_sel = sum(r[3] for r in rows) / n
        pos = sum(1 for r in rows if r[1] > 0)
        print("-" * 92)
        print(f"평균: 당일종가등락 {avg_day:+.1f}% | 당일고가폭 {avg_high:+.1f}% | "
              f"선별가대비종가 {avg_sel:+.1f}% | 종가플러스 {pos}/{n}")
        print("해석: '당일고가폭'이 크면 선별 후에도 추가 상승 여력이 있었던 진짜 주도주.")
        print("      '선별가대비종가'가 음수면 10시 선별 이후 밀린 것(오후 약세).")
    print()


def main():
    if not PICKS_DIR.exists():
        print(f"선별기록 없음: {PICKS_DIR}")
        print("→ 먼저 라이브 장중에 `python leader_finder.py` (10:00 선별)로 기록을 쌓으세요.")
        return
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    files = sorted(PICKS_DIR.glob("*.json"))
    if arg:
        files = [f for f in files if f.stem == arg]
    if not files:
        print("채점할 선별기록 없음")
        return
    for f in files:
        score_file(f)


if __name__ == "__main__":
    main()
