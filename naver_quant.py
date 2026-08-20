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
def _fetch_naver_quant(sosok: int, nxt: bool = False) -> pd.DataFrame:
    """sosok: 0=코스피, 1=코스닥. 반환: code,name,price,change_pct,volume,value_won.

    nxt=False → sise_quant(한국거래소 KRX 정규시장 거래대금).
    nxt=True  → nxt_sise_quant(넥스트레이드 NXT 거래대금). 두 페이지는 컬럼·앵커
      구조가 동일해 같은 파서를 쓴다. 같은 종목의 거래대금은 KRX/NXT 로 분리 집계되며
      (검증: 삼성전자 KRX 6.6조 + NXT 6.1조), 통합 거래대금 = 두 값의 합이다.
    """
    page = "nxt_sise_quant" if nxt else "sise_quant"
    url = f"https://finance.naver.com/sise/{page}.naver?sosok={sosok}"
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


# 브랜드 + 공백 형태의 ETF (예: "TIME 미국나스닥100액티브", "파워 200").
# 공백을 요구해야 파워로직스/파워넷 같은 실제 종목이 걸리지 않는다.
_ETF_BRANDS_SPACED = (
    "TIME", "WON", "MIDAS", "UNICORN", "파워", "KCGI", "DAISHIN",
    "TRUSTON", "마이다스", "에셋플러스",
)

# 리츠/인프라·부동산 펀드 — ETF 는 아니지만 업종이 없고 개별주가 아니다.
# 이름 규칙으로 못 잡는 종목만 코드로 명시(맥쿼리인프라·발해인프라·맵스리얼티·이리츠코크렙).
_FUND_CODES = {"088980", "415640", "094800", "088260"}


def _is_fund_like(code: str, name: str) -> bool:
    """리츠·인프라펀드·액티브ETF 등 개별주가 아닌 상장물이면 True."""
    if code in _FUND_CODES:
        return True
    if name.replace(" ", "").endswith("리츠"):
        return True
    if "액티브" in name:
        return True
    up = name.upper()
    if any(up.startswith(b.upper() + " ") for b in _ETF_BRANDS_SPACED):
        return True
    return False


def _is_common_stock(code: str, name: str) -> bool:
    """보통주만 True: 코드 끝자리 0(우선주 제외) + ETF/ETN/리츠·펀드 이름 제외."""
    if not re.match(r"^\d{5}0$", code):
        return False
    if _is_etf_etn(name):
        return False
    if _is_fund_like(code, name):
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


def _merge_krx_nxt(krx: pd.DataFrame, nxt: pd.DataFrame) -> pd.DataFrame:
    """같은 종목의 KRX+NXT 거래대금·거래량을 합산해 '통합 거래대금'을 만든다.

    KIS 통합(UN) 로직과 동일 개념: value_won(통합) = value_won(KRX) + value_won(NXT).
    가격·등락률·시총은 정규시장(KRX) 값을 우선 쓰고, KRX 에 없는 종목만 NXT 값을 쓴다.
    한쪽 페이지에만 든 종목은 그 한쪽 값만 반영된다(반대 거래소분 미관측) — KIS 의
    거래소별 30행 컷과 같은 경계 한계지만, KRX 단독보다 통합값에 훨씬 근접한다.
    """
    merged: dict[str, dict] = {}
    for src_name, src in (("krx", krx), ("nxt", nxt)):
        if src is None or src.empty:
            continue
        for row in src.to_dict("records"):
            code = row["code"]
            e = merged.get(code)
            if e is None:
                e = dict(row)
                e["src_krx"] = (src_name == "krx")
                e["src_nxt"] = (src_name == "nxt")
                merged[code] = e
            else:
                e["value_won"] = e["value_won"] + row["value_won"]  # 통합 = 합산
                e["volume"] = e["volume"] + row["volume"]
                if not e.get("market_cap") and row.get("market_cap"):
                    e["market_cap"] = row["market_cap"]
                if src_name == "krx":
                    e["src_krx"] = True
                else:
                    e["src_nxt"] = True
    if not merged:
        return pd.DataFrame()
    return pd.DataFrame(list(merged.values()))


def fetch_ranking_unified(top_n: int = 100, stock_only: bool = True) -> pd.DataFrame:
    """네이버 KRX+NXT 통합 거래대금 상위 (KIS 폴백용).

    각 시장(코스피/코스닥)에서 KRX(sise_quant)·NXT(nxt_sise_quant) 순위를 각각
    top_n(기본 100) 씩 받아 종목 코드 기준으로 거래대금을 합산한 뒤, 통합 거래대금
    기준으로 다시 top_n 을 자른다. KRX 단독 순위는 NXT 거래대금(고가주는 KRX 의
    ~90% 규모)을 놓쳐 대형주가 과소계상되는데, 이를 KIS 통합값에 근접하게 복원한다.

    반환 스키마는 fetch_ranking / kis_quant.fetch_ranking 과 동일.
    """
    frames = []
    for sosok in (0, 1):
        try:
            krx = _fetch_naver_quant(sosok, nxt=False)
        except Exception as e:
            print(f"  [네이버 KRX {('코스피' if sosok==0 else '코스닥')} 실패] {e}")
            krx = pd.DataFrame()
        try:
            nxt = _fetch_naver_quant(sosok, nxt=True)
        except Exception as e:
            print(f"  [네이버 NXT {('코스피' if sosok==0 else '코스닥')} 실패] {e}")
            nxt = pd.DataFrame()
        mkt = _merge_krx_nxt(krx, nxt)
        if mkt.empty:
            continue
        if stock_only:
            mask = mkt.apply(lambda r: _is_common_stock(r["code"], r["name"]), axis=1)
            mkt = mkt[mask].copy()
        mkt = mkt.sort_values("value_won", ascending=False).head(top_n)
        frames.append(mkt)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True).drop_duplicates("code")
    return df.sort_values("value_won", ascending=False).reset_index(drop=True)
