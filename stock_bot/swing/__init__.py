# -*- coding: utf-8 -*-
"""일봉 스윙봇 (SWING_BOT_DESIGN.md).

대장주봇(leader_finder.py)과 별개로 도는 봇. 일봉으로 종목을 고르고(bt_swing
전략 재사용), 분봉으로 진입 타점을 잡고, 며칠 보유한다.
드라이런/모의/실전은 전역 TRADE_DRY_RUN·KIS_ENV 로 스톡봇과 같이 갈린다(config.mode_of → orders.place).
"""
