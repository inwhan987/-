# -*- coding: utf-8 -*-
"""셋업별 스코어러.

각 전략은 지표가 붙은 종목 DataFrame을 받아
  signal : bool Series  (True면 '다음날 시가에 진입')
  score  : 0~100 float Series
를 돌려준다.

셋업 유형마다 채점 기준이 다르므로 스코어러를 분리한다.
같은 잣대로 매기면 돌파형과 눌림목형이 둘 다 어중간한 점수를 받는다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import scale

# ── 추세 필터 토글 ───────────────────────────────────────────────────
# 11개 전략이 전부 "상승추세일 것"을 AND 조건으로 걸고 있었다. 그 전제가
# 틀린 구간에서는 11개가 통째로 같이 죽는다. 전제 자체를 빼고 돌려볼 수
# 있어야 '타점이 나쁜 건지 전제가 나쁜 건지'가 구분된다.
USE_TREND = True


def set_trend_filter(flag: bool) -> None:
    global USE_TREND
    USE_TREND = bool(flag)


def _t(cond):
    """추세 전제 조건. USE_TREND=False면 무력화(항상 True)."""
    if USE_TREND:
        return cond
    return pd.Series(True, index=cond.index)

REGISTRY: dict[str, "Strategy"] = {}


class Strategy:
    name = "base"
    desc = ""

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        if getattr(cls, "name", "base") != "base":
            REGISTRY[cls.name] = cls()

    def run(self, d: pd.DataFrame) -> pd.DataFrame:  # pragma: no cover
        raise NotImplementedError

    # 공통 게이트 — '지표가 계산됐는가'만 본다.
    #
    # 유동성(거래대금)·시총 조건은 여기 두지 않는다. 셋업의 품질과 '살 수 있는가'는
    # 다른 문제라서, 점수는 전 종목에 매기고 체결 가능 여부는 engine의 진입
    # 게이트에서 거른다. 여기서 걸러버리면 그 종목이 점수·IC 계산에서 통째로
    # 빠져 유니버스가 좁아지고, 필터가 성과에 도움이 됐는지도 알 수 없게 된다.
    @staticmethod
    def _base_gate(d: pd.DataFrame) -> pd.Series:
        return d["ma20"].notna() & d["ma60"].notna() & d["close"].gt(0)


# ─────────────────────────────────────────────────────────────────────
class Breakout(Strategy):
    name = "BREAKOUT"
    desc = "변동성 수축 후 20일 박스 상단 돌파 + 거래량"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & (d["close"] > d["hh20"])                  # 당일 제외 20일 고가 돌파
            & (d["close"] > d["open"])                  # 종가가 시가 위 (거짓돌파 완화)
            & (d["vol_ratio"] > 1.5)
            & (d["bb_width_pct"].shift(1) < 0.40)       # 직전까지 밴드폭 수축
            & _t(d["ma20"] > d["ma60"])
            & (d["disp20"] < 0.25)                      # 이미 과열된 건 제외
        )
        sc = (
            25 * scale(1 - d["bb_width_pct"].shift(1), 0.5, 1.0)   # 수축 정도
            + 20 * scale(d["vol_ratio"], 1.5, 4.0)
            + 20 * scale(d["ret60"], -0.05, 0.40)
            + 15 * scale(d["near_52w"], 0.75, 1.0)
            + 10 * scale(d["ma60_slope"], -0.02, 0.10)
            + 10 * scale(d["vol_dry"].shift(1), 1.2, 0.6)          # 조정 중 거래량 말랐나
            - 15 * scale(d["disp20"], 0.12, 0.30)                  # 이격 과대 감점
            - 10 * scale(d["upper_wick"], 0.3, 0.7)                # 윗꼬리 감점
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class Pullback(Strategy):
    name = "PULLBACK"
    desc = "정배열 추세 + 20일선 눌림 후 반등 첫 양봉"

    def run(self, d):
        gate = self._base_gate(d)
        touched = (d["low"].rolling(3, min_periods=1).min() <= d["ma20"] * 1.02)
        cond = (
            gate
            & _t(d["aligned"] == 1)
            & _t(d["ma60_slope"] > 0)
            & touched
            & (d["close"] > d["open"])                  # 반등 양봉
            & (d["close"] > d["ma20"])
            & (d["disp20"].between(-0.02, 0.08))
            & (d["ret20"] > -0.15)
        )
        sc = (
            40 * scale(np.log(d["mktcap_eok"].fillna(3000).clip(lower=1)), np.log(3000), np.log(50000))
            + 30 * scale(d["near_52w"], 0.75, 1.0)
            + 30 * scale(np.log(d["value_eok"].fillna(30).clip(lower=1)), np.log(30), np.log(1000))
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class NewHigh(Strategy):
    name = "NEWHIGH"
    desc = "52주 신고가 갱신 + 추세 정합"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & (d["close"] >= d["hh250"])
            & (d["close"] > d["open"])
            & (d["vol_ratio"] > 1.2)
            & _t(d["ma20"] > d["ma60"])
        )
        # 2026-09-22 재설계: vol_ratio(20)·ma60_slope(20) 항목 제거. 2024~26 게이트 통과
        # 청산완료 신호 2,327건에서 둘 다 수익과 역상관(ρ −0.13 / −0.07)이라 맞는 방향인 나머지
        # 항목을 희석시켰다(전체 ρ +0.08 → 제거 후 +0.18, 3개년 모두 +). 남은 원점
        # 60점 만점을 ×100/65 로 늘려 ≥60 게이트 통과율이 종전(≈18%)과 같도록 보정
        # (원점 39/60 이상). 5슬롯 포트폴리오 +152% → +213%, 부트스트랩 99% 우위.
        sc = (
            30 * scale(d["ret120"], 0.0, 0.80)
            + 15 * scale(d["flow_intensity"].fillna(0), 0.0, 0.5)
            + 15 * scale(1 - d["atr_pct_rank"], 0.3, 0.9)
            - 20 * scale(d["disp20"], 0.15, 0.35)
        ) * (100.0 / 65.0)
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class Momentum(Strategy):
    name = "MOMENTUM"
    desc = "20·60일 모멘텀 상위 + 200일선 위 + 60일고가 근접 (매일 스캔 추세형)"

    def run(self, d):
        gate = self._base_gate(d)
        # 2026-09 재점검: 월요일 제한 제거 + 종가≥60일고가×0.98 (돌파 직전·고가 근처 추세 유지).
        # 돌파(>1.0) 추격은 부트 +58/+5로 기각. 60일고가 돌파 전 근접이 +143/+68.
        near_hh = _t(d["close"] >= d["hh60"] * 0.98)
        cond = (
            gate
            & near_hh
            & _t(d["above_ma200"] == 1)
            & _t(d["ret20"] > 0)
            & _t(d["ret60"] > 0)
            & _t(d["ma20"] > d["ma60"])
        )
        # 원점 70 이상만 통과하도록 ×60/70 리스케일 (≥70 부트 +189/+121 vs ≥60 +143/+68).
        sc = (
            35 * scale(d["ret60"], 0.0, 0.50)
            + 25 * scale(d["ret20"], 0.0, 0.25)
            + 20 * scale(d["ma60_slope"], 0.0, 0.12)
            + 20 * scale(1 - d["atr_pct_rank"], 0.2, 0.9)          # 저변동 모멘텀 선호
            - 15 * scale(d["disp20"], 0.15, 0.35)
        ) * (60.0 / 70.0)
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class MeanRev(Strategy):
    name = "MEANREV"
    desc = "장기추세 유지 + RSI(2) 과매도 반등 (단기 평균회귀)"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & _t(d["above_ma200"] == 1)
            & (d["ma200"].notna())
            & (d["rsi2"] < 10)
            & (d["close"] < d["ma5"])
            & (d["ret5"] > -0.20)                       # 급락 종목 제외
        )
        # 2026-09-22 재설계: 구 항목(rsi2·ma60_slope·ret120·disp20)은 수익과 ρ≈0.
        # 3개년 안정 재료 = 시총(+)·낮은 ATR(+)·거래량비(+). ×0.8473 → 60점 = 풀 상위 ~10% (구 통과율 동일)
        sc = (
            40 * scale(np.log(d["mktcap_eok"]), np.log(1000), np.log(50000))
            + 35 * scale(0.07 - d["atr_pct"], 0.0, 0.05)
            + 25 * scale(d["vol_ratio"], 0.5, 2.0)
        ) * 0.8473
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class GapGo(Strategy):
    name = "GAPGO"
    desc = "갭 상승 + 시가 위 마감 (모멘텀 이어달리기)"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & (d["gap"].between(0.02, 0.12))
            & (d["close"] > d["open"])
            & (d["vol_ratio"] > 2.0)
            & _t(d["ma20"] > d["ma60"])
            & (d["close"] > d["hh20"] * 0.98)
        )
        sc = (
            30 * scale(d["vol_ratio"], 2.0, 6.0)
            + 25 * scale(d["body"], 0.2, 0.8)
            + 20 * scale(d["ret60"], 0.0, 0.40)
            + 15 * scale(d["near_52w"], 0.80, 1.0)
            + 10 * scale(d["flow_intensity"].fillna(0), 0.0, 0.5)
            - 20 * scale(d["gap"], 0.08, 0.15)
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


# ── 수급 계열 ────────────────────────────────────────────────────────
class FlowForeign(Strategy):
    name = "FLOW_FORGN"
    desc = "외국인 2일 연속 순매수 첫날 + 중기 상승추세 (수급 선취형)"

    def run(self, d):
        gate = self._base_gate(d)
        # 2026-09 재점검: 연속 3일→2일 (표본 8배), 종가>ma20 제거(ma20>ma60만 유지)
        cond = (
            gate
            & (d["forgn_streak"] >= 2)
            & (d["forgn_streak"].shift(1) < 2)          # 조건 충족 첫날만
            & (d["forgn_intensity"] > 0.03)
            & _t(d["ma20"] > d["ma60"])
        )
        # 산식 교체: 기존 구성항(ret60·ma60_slope·disp20·기관강도)은 성과와 무상관.
        # 유효 재료는 외인강도·52주고가근접·저변동뿐. 원점 56.6 이상만 통과하도록 ×60/56.6 리스케일
        sc = (
            40 * scale(d["forgn_intensity"], 0.03, 0.30)
            + 30 * scale(d["near_52w"], 0.75, 1.0)
            + 30 * scale(1 - d["atr_pct_rank"], 0.2, 0.9)
        ) * (60.0 / 56.6)
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class FlowInst(Strategy):
    name = "FLOW_INST"
    desc = "기관 3일 연속 순매수 첫날 + 중기 상승추세 (저변동·대형주 편향)"

    def run(self, d):
        gate = self._base_gate(d)
        # 2026-09 재점검: 연속일수는 3일 유지(2일로 낮추면 붕괴), 종가>ma20만 제거
        cond = (
            gate
            & (d["inst_streak"] >= 3)
            & (d["inst_streak"].shift(1) < 3)
            & (d["inst_intensity"] > 0.03)
            & _t(d["ma20"] > d["ma60"])
        )
        # 산식 교체: 기존 구성항(ret60·ma60_slope·기관강도·연속일수)은 성과와 무상관이거나
        # 부호가 반대였다(ret60 ρ=-0.10, ma60_slope ρ=-0.08). 기관 수급은 과열되지 않은
        # 저변동·대형주에서만 먹힌다. 원점 45.9 이상만 통과하도록 ×1.31 리스케일
        sc = (
            50 * scale(1 - d["atr_pct_rank"], 0.2, 0.9)
            + 25 * (1 - scale(d["disp20"], 0.0, 0.20))
            + 25 * scale(np.log(d["mktcap_eok"]), np.log(1000), np.log(50000))
        ) * 1.31
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class FlowBoth(Strategy):
    name = "FLOW_BOTH"
    desc = "외국인·기관 동시 순매수 첫날 + 중기 상승추세 (대형·저변동·고가권 편향)"

    def run(self, d):
        gate = self._base_gate(d)
        # 2026-09 재점검: 연속일수는 2일 유지(3일로 올리면 붕괴), 종가>ma20 대신 ma20>ma60
        cond = (
            gate
            & (d["both_streak"] >= 2)
            & (d["both_streak"].shift(1) < 2)
            & (d["flow_intensity"] > 0.05)
            & _t(d["ma20"] > d["ma60"])
        )
        # 산식 교체: 기존 구성항은 무상관이거나 부호가 반대였다(ma60_slope rho=-0.02,
        # both_streak 상수, flow_intensity rho=+0.04). 쌍끌이는 대형·저변동·고가권에서만
        # 먹힌다. 외국인/기관 배합비(균형도 rho=-0.01)는 정보가 아니다. 배율 없음(포화 1.0%)
        sc = (
            40 * scale(np.log(d["mktcap_eok"]), np.log(1000), np.log(50000))
            + 40 * scale(1 - d["atr_pct_rank"], 0.2, 0.9)
            + 20 * scale(d["near_52w"], 0.7, 1.0)
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class FlowPullback(Strategy):
    name = "FLOW_PULLBACK"
    desc = "수급 유입 종목의 눌림목 (수급 × 기술적 결합)"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & _t(d["aligned"] == 1)
            & (d["forgn_sum20"].fillna(0) + d["inst_sum20"].fillna(0) > 0)
            & (d["flow_intensity"] > 0.02)
            & (d["low"].rolling(3, min_periods=1).min() <= d["ma20"] * 1.02)
            # 2026-09-22: 양봉(close>open) 조건 제거 — 신호 PF 동일(1.7)하고 풀이 1.7배 커져 랭킹 선택폭 확대
            & (d["close"] > d["ma20"])
        )
        # 2026-09-23 재검증(재빌드 pkl): 09-22 개정본도 살아났으나(ρ +0.041) 아래 산식이 더 낫다.
        # 저변동을 raw atr_pct 대신 atr_pct_rank(120일 시계열 백분위)로 바꾸고 가중치를 재배분.
        # 통과수 일치 기준 PF: 시가 1.71→1.83, +0.2% 1.82→1.99, 실측체결 +2.4% 1.75→1.97.
        # 모든 체결가 가정에서 PF·기대수익 우위 = 체결편향에 강건. NaN 은 0점으로 떨어지게 둔다.
        # 보정계수는 두지 않는다: 60컷 통과율 33.3%→26.1% 로 좁아지지만 무보정이 전 지표 우위
        # (실측체결 +2.4%: PF 2.01→2.21, 포트 p10 +14.7→+18.2).
        sc = (
            40 * scale((1.0 - d["atr_pct_rank"]).fillna(0.0), 0.2, 0.9)
            + 30 * scale(d["near_52w"].fillna(0.0), 0.75, 1.0)
            + 30 * scale(np.log(d["mktcap_eok"].fillna(3000).clip(lower=1)), np.log(3000), np.log(50000))
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class ValueMom(Strategy):
    name = "VALUE_MOM"
    desc = "저PER·흑자 + 모멘텀 (밸류 축이 실제로 기여하는지 확인용)"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & (d["eps_pos"] == 1)
            & (d["per"].between(1, 15))
            & (d["pbr"].between(0.2, 2.0))
            & _t(d["above_ma200"] == 1)
            & (d["close"] > d["hh20"])
            & (d["vol_ratio"] > 1.3)
        )
        # 2026-09-23 재설계: 구 산식은 수익과 역상관이었다(ρ -0.038, 버킷 단조 하락 1.87→0.93).
        # 항목별 ρ - PER -0.09 / PBR -0.03 은 부호가 맞았으나 ret60 -0.14 / vol_ratio -0.12 /
        # ma60_slope -0.11 로 모멘텀 50점이 전부 반대 부호였고 그쪽이 밸류 절반을 눌렀다.
        # 부호 맞는 저PER 만 남기고 다른 전략에서 검증된 저변동·시총으로 교체.
        # 통과수 일치 PF: VALUE_PURE 1.33->3.96 / VALUE_MOM 1.18->4.40, 전 체결가 가정에서 우위.
        sc = (
            30 * scale(15 - d["per"], 0, 12)
            + 35 * scale((1.0 - d["atr_pct_rank"]).fillna(0.0), 0.2, 0.9)
            + 35 * scale(np.log(d["mktcap_eok"].fillna(3000).clip(lower=1)), np.log(3000), np.log(50000))
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


class ValuePure(Strategy):
    name = "VALUE_PURE"
    desc = "VALUE_MOM 에서 돌파 조건(close>hh20)만 뺀 것 — 스윙봇 hold 형(2026-09-14)"

    def run(self, d):
        gate = self._base_gate(d)
        cond = (
            gate
            & (d["eps_pos"] == 1)
            & (d["per"].between(1, 15))
            & (d["pbr"].between(0.2, 2.0))
            & _t(d["above_ma200"] == 1)
            & (d["vol_ratio"] > 1.3)
        )
        # 2026-09-23 재설계: 구 산식은 수익과 역상관이었다(ρ -0.038, 버킷 단조 하락 1.87→0.93).
        # 항목별 ρ - PER -0.09 / PBR -0.03 은 부호가 맞았으나 ret60 -0.14 / vol_ratio -0.12 /
        # ma60_slope -0.11 로 모멘텀 50점이 전부 반대 부호였고 그쪽이 밸류 절반을 눌렀다.
        # 부호 맞는 저PER 만 남기고 다른 전략에서 검증된 저변동·시총으로 교체.
        # 통과수 일치 PF: VALUE_PURE 1.33->3.96 / VALUE_MOM 1.18->4.40, 전 체결가 가정에서 우위.
        sc = (
            30 * scale(15 - d["per"], 0, 12)
            + 35 * scale((1.0 - d["atr_pct_rank"]).fillna(0.0), 0.2, 0.9)
            + 35 * scale(np.log(d["mktcap_eok"].fillna(3000).clip(lower=1)), np.log(3000), np.log(50000))
        )
        return pd.DataFrame({"signal": cond.fillna(False),
                             "score": np.clip(sc, 0, 100)}, index=d.index)


def get(names: list[str] | None = None) -> dict[str, Strategy]:
    if not names:
        return dict(REGISTRY)
    out = {}
    for n in names:
        key = n.upper()
        if key not in REGISTRY:
            raise SystemExit(f"모르는 전략: {n}\n사용 가능: {', '.join(REGISTRY)}")
        out[key] = REGISTRY[key]
    return out
