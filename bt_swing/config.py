# -*- coding: utf-8 -*-
"""백테스트 전역 설정."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ── 캐시 위치 ──────────────────────────────────────────────────────────
CACHE_DIR = Path(os.environ.get("BT_SWING_CACHE", "data/bt_swing_cache"))
OUT_DIR = Path(os.environ.get("BT_SWING_OUT", "data/bt_swing_out"))


@dataclass
class Costs:
    """거래비용. 연도·시장별로 다르니 실제 값으로 조정할 것."""

    buy_fee: float = 0.00015          # 매수 수수료 0.015%
    sell_fee: float = 0.00015         # 매도 수수료 0.015%
    sell_tax: float = 0.0015          # 증권거래세(농특세 포함) 0.15% — 연도별 확인 필요
    slippage: float = 0.0010          # 시가 체결 슬리피지 편도 0.10%

    def buy_cost(self) -> float:
        return self.buy_fee + self.slippage

    def sell_cost(self) -> float:
        return self.sell_fee + self.sell_tax + self.slippage


@dataclass
class UniverseCfg:
    start: str = "20210101"
    end: str = "20250831"
    # 후보 종목 추출: 분기별 스냅샷에서 거래대금이 이 값을 넘은 적 있는 종목
    min_value_eok: float = 30.0       # 일 거래대금 하한 (억)
    min_price: int = 1000             # 동전주 배제
    max_price: int = 1_000_000
    # 후보 상한 (테스트 속도 조절용). None이면 전부.
    # 상한을 두면 '최초 관측 시점 거래대금' 순으로 자른다(미래참조 방지).
    max_tickers: int | None = None
    markets: tuple[str, ...] = ("KOSPI", "KOSDAQ")
    exclude_spac: bool = True
    exclude_preferred: bool = True


@dataclass
class PortfolioCfg:
    initial_capital: float = 100_000_000.0
    max_positions: int = 10           # 동시 보유 상한
    max_new_per_day: int = 3          # 하루 신규 진입 상한
    position_pct: float = 0.10        # 종목당 자본 비중 (1/max_positions 권장)
    max_per_sector: int = 3           # (섹터 데이터 있을 때만 적용)


@dataclass
class ExitCfg:
    """등급별로 덮어쓰는 기본 청산 파라미터."""

    take_profit: float = 0.08         # +8%
    stop_loss: float = 0.04           # -4%
    time_stop_days: int = 15          # N영업일 경과 시 종가 청산
    use_atr_stop: bool = True         # ATR 기반 손절 사용
    # 1차 실행에서 atr_stop_mult=2.0이 상한 8%에 걸려 사실상 전 종목 -8% 손절이
    # 됐고, trail_after=0.05가 익절(+12%)보다 먼저 터져 이익이 +1.9%에서 잘렸다.
    # 손익비가 1 근처로 눌린 원인이라 손절은 좁히고 트레일링은 뒤로 미룬다.
    atr_stop_mult: float = 1.3
    atr_tp_mult: float = 3.0
    trail_after: float = 0.08         # 익절 목표의 60~70% 지점에서 발동
    trail_pct: float = 0.05           # 고점 대비 -5% 이탈 시 청산
    use_trailing: bool = True


@dataclass
class RegimeCfg:
    enabled: bool = True
    index_code: str = "1001"          # KOSPI. 코스닥은 "2001"
    ma: int = 200                     # 지수 200일선
    # 지수가 MA 아래일 때: 0.0이면 신규 진입 전면 중단, 0.5면 사이즈 절반
    below_size_mult: float = 0.0


@dataclass
class BacktestCfg:
    universe: UniverseCfg = field(default_factory=UniverseCfg)
    portfolio: PortfolioCfg = field(default_factory=PortfolioCfg)
    exits: ExitCfg = field(default_factory=ExitCfg)
    regime: RegimeCfg = field(default_factory=RegimeCfg)
    costs: Costs = field(default_factory=Costs)

    # 상한가/하한가 회피
    limit_move: float = 0.295         # ±29.5% 이상 갭이면 체결 불가로 간주

    # ── 진입 게이트 ────────────────────────────────────────────────
    # 점수는 전 종목에 매기고, '실제로 살 수 있는가'는 여기서 거른다.
    # 셋업이 아무리 좋아도 거래대금 2억짜리는 체결이 안 되므로, 이걸 빼면
    # 백테스트에 실전에 없는 수익이 잡힌다.
    entry_min_value_eok: float = 30.0    # 신호일 20일 평균 거래대금 하한 (억)
    entry_min_mktcap_eok: float = 5000.0  # 시가총액 하한 (억) — 휘둘리는 소형주 배제
    # 2026-09-23: 1000→5000. 10전략 중 8개가 5,000억 미만에서 PF<1 이었고
    # 단계별(1000 +53.9 / 3000 +84.9 / 5000 +87.1 / 8000 +86.6) 로 3,000억부터 고원.
    # 라이브(SWING_ENTRY_MIN_CAP_EOK)와 같은 값으로 맞춰 둔다.
    # 전략별 원점수 하한 — 라이브 SWING_ENTRY_MIN_RAW_BY_STRATEGY 와 같은 값이어야 한다.
    # 없는 전략은 컷 없음. 나머지 7전략은 컷 60(전역)이 최적이라 비워 둔다.
    entry_min_raw_by_strategy: dict[str, float] = field(default_factory=lambda: {
        "PULLBACK": 80.0, "FLOW_FORGN": 70.0, "GAPGO": 80.0,
        "VALUE_MOM": 70.0, "VALUE_PURE": 70.0,
    })
    # 시총을 모르는 종목을 어떻게 할지. True면 진입 차단(안전), False면 통과.
    # 수집이 통째로 실패했을 때 필터가 조용히 무력화되는 사고를 막는다.
    block_when_cap_missing: bool = True
    entry_min_price: int = 1000          # 동전주 배제
    entry_max_atr_pct: float = 0.15      # 하루 변동 15% 넘는 과열주 배제
    # 주문 크기가 그날 거래대금에서 차지하는 비중 상한.
    # 이걸 넘으면 내 주문이 가격을 밀어버리므로 체결 불가로 본다.
    max_order_share_of_value: float = 0.01
