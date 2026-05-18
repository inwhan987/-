"""거래 로그의 매수 가격을 KIS 실제 평단가로 보정.

사용:
  python tools/fix_trade_price.py [trade_id]
  python tools/fix_trade_price.py 005930      # 종목의 최신 매수 기록 자동 보정

옵션 없이 실행:
  python tools/fix_trade_price.py             # 모든 종목의 최신 매수 기록 보정
"""
from __future__ import annotations
import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from stock_bot.broker import KISBroker
from stock_bot.storage.db import ENGINE, TradeLog


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    # KIS 현재 평단가 조회
    print("KIS 포지션 조회 중...", flush=True)
    broker = KISBroker()
    try:
        positions = {row["pdno"]: float(row.get("pchs_avg_pric", 0) or 0)
                     for row in broker.get_positions()
                     if int(row.get("hldg_qty", 0) or 0) > 0}
    finally:
        broker.close()

    print(f"KIS 평단가: {positions}\n")
    if not positions:
        print("⚠️  현재 KIS에 포지션이 없습니다. 보정할 매수 기록 찾을 수 없음.")
        return

    with Session(ENGINE) as s:
        # 국내 종목코드는 6자리 (예: 005930). 그 외 숫자는 trade_id.
        is_symbol = arg and len(arg) == 6 and arg.isdigit()
        is_trade_id = arg and arg.isdigit() and not is_symbol

        if is_trade_id:
            # trade_id 로 단건 보정
            tid = int(arg)
            row = s.get(TradeLog, tid)
            if row is None:
                print(f"❌ trade_id={tid} 없음")
                return
            if row.symbol not in positions:
                print(f"❌ {row.symbol} 포지션 없음 (이미 매도?)")
                return
            new_price = positions[row.symbol]
            old_price = row.price
            print(f"매수 ID {tid}: {row.symbol} {row.quantity}주")
            print(f"  기존 가격: {old_price:,.0f}원")
            print(f"  새 가격:   {new_price:,.0f}원 (KIS 평단가)")
            if abs(new_price - old_price) < 1:
                print("  → 차이 없음, 스킵")
                return
            if input("  적용하시겠습니까? (y/N): ").lower() != "y":
                print("  취소")
                return
            row.price = new_price
            s.commit()
            print("  ✅ 업데이트 완료")
            return

        # 종목 코드(또는 전체)로 최신 매수 보정
        symbols = [arg] if arg else list(positions.keys())
        for sym in symbols:
            if sym not in positions:
                print(f"❌ {sym} KIS 포지션 없음, 스킵")
                continue
            new_price = positions[sym]
            # 해당 종목 최신 buy 거래 찾기
            row = s.scalars(
                select(TradeLog)
                .where(TradeLog.symbol == sym, TradeLog.side == "buy")
                .order_by(desc(TradeLog.ts))
                .limit(1)
            ).first()
            if row is None:
                print(f"❌ {sym} 매수 기록 없음")
                continue
            old_price = row.price
            print(f"{sym} 최신 매수 ID {row.id}: {row.quantity}주 @ {old_price:,.0f}원 → {new_price:,.0f}원", end=" ")
            if abs(new_price - old_price) < 1:
                print("(차이 없음, 스킵)")
                continue
            row.price = new_price
            s.commit()
            print(f"✅ 업데이트 (차이 {new_price - old_price:+,.0f}원)")


if __name__ == "__main__":
    main()
