# -*- coding: utf-8 -*-
"""스윙봇 장중 실행 (명세 13절 9). 모드(dryrun/paper/live) 는 전역 TRADE_DRY_RUN·KIS_ENV 를 따른다.

    python scripts/swing_live.py [--date YYYYMMDD]

장 시작 전(08:50 전후)에 띄운다. 감시 리스트는 전날 nightly 가 ok 인 경우에만 쓰고,
아니면 보유 종목 청산 감시만 한다(명세 11절).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from loguru import logger  # noqa: E402

from stock_bot.swing import live  # noqa: E402


def main() -> None:
    # swing-bot 컨테이너(./logs:/app/logs) — nightly 와 같은 파일 → 웹 로그탭 '스윙봇'.
    # (전에는 stdout 뿐이라 장중 WS 끊김·백필 원인을 웹에서 볼 수 없었다)
    if Path("/app/logs").is_dir():
        logger.add("/app/logs/stock_swing.log", rotation="10 MB", retention=10, buffering=1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="거래일 YYYYMMDD (기본 오늘)")
    a = ap.parse_args()
    live.main(a.date)


if __name__ == "__main__":
    main()
