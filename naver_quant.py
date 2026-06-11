"""네이버 거래대금 순위 공용 모듈 — 대장주 선별(leader_finder)과 섹터분석(market_analysis) 공유.

- _fetch_naver_quant : 네이버 sise_quant 거래대금 순위 1페이지 (시장당 ~100종목 고정,
  page 파라미터는 네이버가 무시)
- fetch_ranking      : 코스피/코스닥 각각 top_n 합산 (보통주 필터 옵션)
- _is_common_stock   : 보통주 판별 (코드 끝자리 0 + ETF/ETN 이름 제외)

함수 동작은 leader_finder.py 에 있던 원본과 동일 — 위치만 이동.
부작용 없는 순수 모듈 (stdout 재설정 등 모듈레벨 사이드이펙트 금지:
market_analysis --json 모드가 stdout redirect 상태에서 import 할 수 있음).
"""
from __future__ import annotations

import io
import re

import requests
import pandas as pd

_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── 네이버 거래대금 순위 (코스피+코스닥) ─────────────────────────────
def _fetch_naver_quant(sosok: int) -> pd.DataFrame:
    """sosok: 0=코스피, 1=코스닥. 반환: code,name,price,change_pct,volume,value_won."""
    url = f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}"
    r = requests.get(url, headers=_HDR, timeout=10)
    r.encoding = "euc-kr"

    # 종목 행 앵커에서 code+name (순서 보존)
    pairs = re.findall(
        r"/item/main\.naver\?code=(\d{6})\" class=\"tltle\">([^<]+)<", r.text
    )
    name2code = {n: c for c, n in pairs}

    tables = pd.read_html(io.StringIO(r.text))
    # 데이터 테이블: N 컬럼 + 종목명/등락률/거래대금 포함
    df = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        if "N" in cols and "종목명" in cols and "거래대금" in cols:
            df = t
            break
    if df is None:
        return pd.DataFrame()

    df = df[df["N"].notna()].copy()
    cols = [str(c) for c in df.columns]
    has_mktcap = "시가총액" in cols
    rows = []
    for _, row in df.iterrows():
        name = str(row["종목명"]).strip()
        code = name2code.get(name)
        if not code:
            continue
        try:
            price = float(str(row["현재가"]).replace(",", ""))
            chg = float(str(row["등락률"]).replace("%", "").replace("+", "").replace(",", ""))
            vol = float(str(row["거래량"]).replace(",", ""))
            val_m = float(str(row["거래대금"]).replace(",", ""))  # 백만원
            mktcap = (float(str(row["시가총액"]).replace(",", "")) * 100_000_000
                      if has_mktcap else 0.0)          # 억원 → 원
        except Exception:
            continue
        rows.append({
            "code": code, "name": name, "price": price,
            "change_pct": chg, "volume": vol,
            "value_won": val_m * 1_000_000,  # 백만원 → 원
            "market_cap": mktcap,             # 원 단위
            "market": "KOSPI" if sosok == 0 else "KOSDAQ",
        })
    return pd.DataFrame(rows)


# ETF/ETN 브랜드 접두 (이름 기반 제외). pykrx 티커목록이 빈 값 반환 → 코드/이름 규칙 사용
_ETF_PREFIXES = (
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "ACE", "SOL", "PLUS", "RISE",
    "KIWOOM", "HANARO", "TIMEFOLIO", "KOACT", "KOSEF", "FOCUS", "WOORI",
    "1Q", "BNK", "HK", "마이다스", "마이티", "히어로즈", "삼성", "TREX",
)


def _is_etf_etn(name: str) -> bool:
    up = name.upper().replace(" ", "")
    if "ETN" in up:
        return True
    # '삼성' 은 삼성전자 등 일반주와 충돌 → ETF 키워드 동반 시에만 제외
    for p in _ETF_PREFIXES:
        if up.startswith(p.upper()):
            if p == "삼성" and not any(k in name for k in ("레버리지", "인버스", "선물", "TR", "ETN")):
                continue
            return True
    return False


def _is_common_stock(code: str, name: str) -> bool:
    """보통주만 True: 코드 끝자리 0(우선주 제외) + ETF/ETN 이름 제외."""
    if not re.match(r"^\d{5}0$", code):
        return False
    if _is_etf_etn(name):
        return False
    return True


def fetch_ranking(top_n: int = 100, stock_only: bool = True) -> pd.DataFrame:
    """코스피/코스닥 각각 top_n 상위 후 합산 (보통주만).

    코스피+코스닥 합산 후 자르면 코스닥 종목이 코스피 대형주에 밀려
    상위 N에서 탈락하는 문제를 방지하기 위해 각 시장별로 top_n씩 가져온다.
    """
    frames = []
    for sosok in (0, 1):
        try:
            mkt_df = _fetch_naver_quant(sosok)
            if not mkt_df.empty:
                if stock_only:
                    mask = mkt_df.apply(
                        lambda r: _is_common_stock(r["code"], r["name"]), axis=1)
                    mkt_df = mkt_df[mask].copy()
                mkt_df = mkt_df.sort_values(
                    "value_won", ascending=False).head(top_n)
                frames.append(mkt_df)
        except Exception as e:
            print(f"  [네이버 {('코스피' if sosok==0 else '코스닥')} 실패] {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("code")
    return df.reset_index(drop=True)
