# -*- coding: utf-8 -*-
"""업종(섹터) 매핑 — KRX 업종지수 구성종목 기준.

네이버 업종 크롤링은 '현재 시점' 스냅샷이라 과거 백테스트에 그대로 쓰면
약한 미래참조가 된다(특히 테마는 시점에 따라 생기고 사라짐).
KRX 업종지수는 날짜를 넣으면 그 시점 구성종목을 주므로 시점 정합이 맞는다.

실패해도 백테스트는 그대로 돈다 — 섹터를 모르면 'UNKNOWN'으로 두고
섹터 제한만 적용되지 않는다.
"""
from __future__ import annotations

import time
import bisect

import pandas as pd

from .config import CACHE_DIR
from .data import (_cache_path, _load, _save, _retry, _lazy_stock, trading_days,
                   Blocked)

# 업종이 아닌 지수(시장대표·규모별·스타일·테마)를 걸러내기 위한 키워드
_EXCLUDE_KW = (
    "코스피", "코스닥", "KRX", "대형주", "중형주", "소형주",
    "200", "100", "50", "배당", "가치", "성장", "우량", "ESG",
    "지배구조", "저변동", "모멘텀", "인버스", "레버리지", "TOP",
)
# 구성종목이 이보다 많으면 업종지수가 아니라 시장/규모 지수로 본다
_MAX_MEMBERS = 300


def _industry_indices(date: str, market: str) -> list[tuple[str, str]]:
    """(지수코드, 지수명) 중 업종으로 보이는 것만."""
    cache = _cache_path("sector", f"idxlist_{market}_{date}.pkl")
    hit = _load(cache)
    if hit is not None:
        return hit
    stock = _lazy_stock()
    out: list[tuple[str, str]] = []
    try:
        codes = _retry(stock.get_index_ticker_list, date, market)
    except Blocked:
        raise  # 차단을 빈 목록으로 캐시하면 영영 그 날짜 섹터가 비어 버린다
    except Exception as e:  # noqa: BLE001
        print(f"  [섹터] 지수목록 실패 {market}/{date}: {str(e)[:60]}")
        return out  # 실패는 캐시하지 않는다 — 다음 실행에서 다시 받는다
    for c in codes or []:
        try:
            nm = stock.get_index_ticker_name(c)
        except Exception:  # noqa: BLE001
            continue
        if not nm:
            continue
        if any(k in nm for k in _EXCLUDE_KW):
            continue
        out.append((str(c), str(nm)))
    _save(cache, out)
    return out


def _members(date: str, index_code: str) -> list[str]:
    cache = _cache_path("sector", f"members_{index_code}_{date}.pkl")
    hit = _load(cache)
    if hit is not None:
        return hit
    stock = _lazy_stock()
    try:
        m = _retry(stock.get_index_portfolio_deposit_file, index_code, date)  # pykrx 시그니처는 (ticker, date)
        m = [str(x) for x in (m or [])]
    except Blocked:
        raise
    except Exception:  # noqa: BLE001
        return []  # 실패는 캐시하지 않는다
    if not m:
        return []  # 빈 결과도 캐시하지 않는다 (인자 오류·일시 장애가 영구화되는 것 방지)
    _save(cache, m)
    return m


class SectorMap:
    """스냅샷 날짜별 종목→업종 매핑. 조회 시점 이전의 가장 최근 스냅샷을 쓴다."""

    def __init__(self, snaps: dict[str, dict[str, str]]):
        self.snaps = snaps
        self.dates = sorted(snaps.keys())
        self._ts = [pd.Timestamp(d) for d in self.dates]

    def __len__(self) -> int:
        return len(self.dates)

    def coverage(self) -> float:
        if not self.dates:
            return 0.0
        last = self.snaps[self.dates[-1]]
        return float(len(last))

    def get(self, ticker: str, date) -> str:
        if not self.dates:
            return "UNKNOWN"
        ts = pd.Timestamp(date)
        i = bisect.bisect_right(self._ts, ts) - 1
        if i < 0:
            i = 0
        # 해당 스냅샷에 없으면 이전 스냅샷들을 거슬러 올라가며 찾는다
        for j in range(i, -1, -1):
            s = self.snaps[self.dates[j]].get(ticker)
            if s:
                return s
        for j in range(i + 1, len(self.dates)):
            s = self.snaps[self.dates[j]].get(ticker)
            if s:
                return s
        return "UNKNOWN"


def build(start: str, end: str, every: int = 60, verbose: bool = True,
          budget_sec: float = 0.0) -> SectorMap | None:
    """분기(약 60영업일) 간격으로 업종지수 구성종목 스냅샷을 뜬다.

    budget_sec > 0 이면 그 시간이 지나는 순간 멈추고 None 을 돌려준다
    (--download-only 용). 지수목록·구성종목은 호출 단위로 캐시되므로 다음
    실행은 받은 곳부터 이어진다. 완성된 맵만 map_*.pkl 로 저장한다.
    """
    cache = _cache_path("sector", f"map_{start}_{end}_{every}.pkl")
    hit = _load(cache)
    if hit is not None:
        return SectorMap(hit)

    days = trading_days(start, end)
    if not days:
        return SectorMap({})
    picks = days[::every]
    if days[-1] not in picks:
        picks.append(days[-1])

    snaps: dict[str, dict[str, str]] = {}
    t0 = time.time()
    def _expired() -> bool:
        return budget_sec > 0 and (time.time() - t0) > budget_sec

    for i, d in enumerate(picks, 1):
        m: dict[str, str] = {}
        for mkt in ("KOSPI", "KOSDAQ"):
            for code, name in _industry_indices(d, mkt):
                if _expired():
                    if verbose:
                        print(f"  [섹터] 시간 예산 소진 — 스냅샷 {i-1}/{len(picks)} "
                              f"완료 지점에서 중단합니다")
                    return None
                mem = _members(d, code)
                if not mem or len(mem) > _MAX_MEMBERS:
                    continue
                for tk in mem:
                    m.setdefault(tk, name)
        snaps[d] = m
        if verbose:
            print(f"  [섹터] {d}  {len(m)}종목 매핑  "
                  f"({i}/{len(picks)}, {(time.time()-t0)/60:.1f}분)", flush=True)
    _save(cache, snaps)
    return SectorMap(snaps)


# ── 쏠림 진단 ─────────────────────────────────────────────────────────
def concentration(trades: pd.DataFrame, smap: SectorMap,
                  calendar: pd.DatetimeIndex) -> dict:
    """체결 내역에서 '동시 보유 중 같은 업종이 몇 개였나'를 복원한다.

    섹터 제한을 걸지 말지는 이 수치를 보고 정하면 된다.
    자연히 업종당 2종목 이하였다면 제한 자체가 필요 없다.
    """
    if trades is None or not len(trades) or not len(smap):
        return {}
    tr = trades.copy()
    tr["entry"] = pd.to_datetime(tr["entry_date"])
    tr["exit"] = pd.to_datetime(tr["exit_date"])
    tr["sector"] = [smap.get(t, d) for t, d in zip(tr["ticker"], tr["entry"])]

    rows = []
    for day in calendar:
        held = tr[(tr["entry"] <= day) & (tr["exit"] >= day)]
        if not len(held):
            continue
        vc = held["sector"].value_counts()
        rows.append({"date": day, "보유": len(held),
                     "최대동일업종": int(vc.iloc[0]),
                     "업종수": int(len(vc)),
                     "최대업종": str(vc.index[0])})
    if not rows:
        return {}
    c = pd.DataFrame(rows)
    heavy = c[c["보유"] >= 3]
    return {
        "일수": len(c),
        "평균보유": float(c["보유"].mean()),
        "평균최대동일업종": float(c["최대동일업종"].mean()),
        "최대동일업종_최댓값": int(c["최대동일업종"].max()),
        "3종목이상인날_평균집중도": (float((heavy["최대동일업종"] / heavy["보유"]).mean())
                                     if len(heavy) else float("nan")),
        "업종비중_상위": tr["sector"].value_counts().head(8).to_dict(),
        "UNKNOWN비율": float((tr["sector"] == "UNKNOWN").mean()),
        "일별": c,
    }
