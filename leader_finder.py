"""대장주(섹터 리더) 탐색기 — 장중 거래대금 상위에서 주도 섹터/종목 추출.

알고리즘 (사용자 설계):
  1) 장 시작 후, 거래대금 상위 100 (코스피+코스닥 통합)
  2) 그중 많이 상승하는 종목 추림 (등락률 >= RISE_MIN_PCT)
  3) 추려진 상승 종목들의 섹터(네이버 업종) 집계 → 주도(핫) 섹터 식별
  4) 각 핫섹터 안에서 상승률 1위 종목 선정,
     단 거래대금이 평소(최근 5거래일 평균) 대비 VOL_MULT 배 이상일 것
     (장중이므로 세션 경과 비율로 평균을 보정해 비교)

데이터 소스:
  - 거래대금 순위/등락률/현재가 : 네이버 금융 sise_quant (장중, 무료)
  - 종목 유니버스 필터(ETF/ETN/우선주 제외) : 코드/이름 규칙 (pykrx 티커목록
    엔드포인트가 빈 값을 반환해 사용 불가 → 보통주=코드 끝자리 0,
    ETF/ETN=브랜드 접두 이름으로 제외)
  - 5일 평균 거래대금            : pykrx 일봉(거래량×종가 근사) — pykrx OHLCV에
    거래대금 컬럼이 없어 거래량×종가로 일중 거래대금을 근사 (일 1회, 디스크 캐시)
  - 업종(섹터)                   : 네이버 coinfo 업종 (캐시)

※ 기존 screener.py 와 완전 독립 (import 안 함).
※ KIS 미사용 — 선별된 소수 대장주의 분봉/호가 확인은 별도 전략에서 KIS 로.

사용:
  python leader_finder.py                 # 10:00 까지 대기 후 1회 선별(기본)
  python leader_finder.py --at 10:30      # 10:30 에 선별
  python leader_finder.py --once          # 지금 즉시 1회(테스트)
  python leader_finder.py --once --ignore-hours --rise-min 2.5 --vol-mult 3
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

HERE = Path(__file__).parent
_CACHE_DIR = HERE / "data"
_AVGVAL_CACHE_PATH = _CACHE_DIR / "leader_avgval_cache.json"

_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# 세션: 09:00 ~ 15:30 (390분)
_SESSION_START = (9, 0)
_SESSION_END = (15, 30)
_SESSION_MIN = 390

# ── 캐시 ────────────────────────────────────────────────────────────
_SECTOR_CACHE: dict[str, str] = {}        # code -> 업종명
_GROUP_CACHE: dict[str, str] = {}         # upjong_no -> 업종명
_AVGVAL_CACHE: dict[str, dict] = {}       # code -> {"date": "YYYYMMDD", "avg": float}
_UNIVERSE_CACHE: dict[str, set] = {}      # "stocks" -> set(code)


def _load_avgval_cache() -> None:
    global _AVGVAL_CACHE
    try:
        if _AVGVAL_CACHE_PATH.exists():
            _AVGVAL_CACHE = json.loads(_AVGVAL_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _AVGVAL_CACHE = {}


def _save_avgval_cache() -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _AVGVAL_CACHE_PATH.write_text(
            json.dumps(_AVGVAL_CACHE, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass


# ── 1) 네이버 거래대금 순위 (코스피+코스닥) ─────────────────────────
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


# ── 2) 5일 평균 거래대금 (pykrx, 일 1회 캐시) ───────────────────────
def avg_value_5d(code: str) -> float:
    """최근 5거래일 평균 일중 거래대금(원). 실패 시 0.0.

    pykrx OHLCV 에 거래대금 컬럼이 없어 거래량×종가로 근사한다.
    """
    today = datetime.now().strftime("%Y%m%d")
    c = _AVGVAL_CACHE.get(code)
    if c and c.get("date") == today and c.get("avg", 0) > 0:
        return float(c["avg"])
    avg = 0.0
    try:
        from pykrx import stock as krx
        end = datetime.now()
        start = end - timedelta(days=21)
        df = krx.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
        )
        if df is not None and not df.empty and {"거래량", "종가"} <= set(df.columns):
            val = (df["거래량"].astype(float) * df["종가"].astype(float))
            val = val[val > 0]
            if len(val) >= 2:
                # 당일(마지막 행, 미완성) 제외 → 직전 최대 5거래일 평균
                hist = val.iloc[:-1]
                avg = float(hist.tail(5).mean()) if len(hist) >= 1 else 0.0
    except Exception:
        avg = 0.0
    if avg > 0:
        _AVGVAL_CACHE[code] = {"date": today, "avg": avg}
    return avg


# ── 3) 섹터(네이버 업종) ─────────────────────────────────────────────
def _naver_group_name(upjong_no: str) -> str:
    if upjong_no in _GROUP_CACHE:
        return _GROUP_CACHE[upjong_no]
    name = ""
    try:
        url = (f"https://finance.naver.com/sise/sise_group_detail.naver"
               f"?type=upjong&no={upjong_no}")
        r = requests.get(url, headers=_HDR, timeout=10)
        r.encoding = "euc-kr"
        m = re.search(r"<title>\s*([^:<\n]+?)\s*(?::\s*Npay|</title>)", r.text)
        if m:
            name = m.group(1).strip()
    except Exception:
        pass
    _GROUP_CACHE[upjong_no] = name
    return name


def sector_of(code: str) -> str:
    if code in _SECTOR_CACHE:
        return _SECTOR_CACHE[code]
    sec = ""
    try:
        url = f"https://finance.naver.com/item/coinfo.naver?code={code}"
        r = requests.get(url, headers=_HDR, timeout=10)
        r.encoding = "euc-kr"
        m = re.search(r"upjong&no=(\d+)", r.text)
        if m:
            sec = _naver_group_name(m.group(1))
    except Exception:
        pass
    _SECTOR_CACHE[code] = sec or "(미상)"
    return _SECTOR_CACHE[code]


# ── 4) 네이버 테마 ──────────────────────────────────────────────────
_THEME_STOCK_CACHE: dict[str, set] = {}   # theme_no -> set(code)


def fetch_theme_list(min_change: float = 3.0) -> list[dict]:
    """당일 등락률 min_change% 이상인 핫테마 목록 반환.

    반환: [{"no": "505", "name": "로봇", "change_pct": 6.83}, ...]
    """
    url = "https://finance.naver.com/sise/theme.naver"
    try:
        r = requests.get(url, headers=_HDR, timeout=10)
        r.encoding = "euc-kr"
    except Exception:
        return []

    # 테마번호+이름
    nos = re.findall(r"type=theme&no=(\d+)[^>]*>([^<]+)</a>", r.text)

    # 등락률 파싱 (테이블 첫 번째)
    try:
        tables = pd.read_html(io.StringIO(r.text))
        tbl = None
        for t in tables:
            cols = [str(c) for c in t.columns]
            if any("등락" in c or "대비" in c for c in cols):
                tbl = t
                break
        if tbl is None:
            tbl = tables[0]
        # 두 번째 컬럼(전일대비 등락률) 파싱
        chg_col = tbl.iloc[:, 1].astype(str)
        chg_vals = chg_col.str.replace("%", "").str.replace("+", "").str.replace(",", "")
        chg_vals = pd.to_numeric(chg_vals, errors="coerce")
    except Exception:
        return []

    results = []
    for i, (no, name) in enumerate(nos):
        if i >= len(chg_vals):
            break
        chg = chg_vals.iloc[i] if i < len(chg_vals) else float("nan")
        if pd.isna(chg) or chg < min_change:
            continue
        results.append({"no": no, "name": name.strip(), "change_pct": float(chg)})

    results.sort(key=lambda x: x["change_pct"], reverse=True)
    return results


def fetch_theme_stocks(theme_no: str) -> set:
    """테마 상세 페이지에서 종목코드 집합 반환 (캐시)."""
    if theme_no in _THEME_STOCK_CACHE:
        return _THEME_STOCK_CACHE[theme_no]
    codes: set = set()
    try:
        url = (f"https://finance.naver.com/sise/sise_group_detail.naver"
               f"?type=theme&no={theme_no}")
        r = requests.get(url, headers=_HDR, timeout=10)
        r.encoding = "euc-kr"
        codes = set(re.findall(r"code=(\d{6})", r.text))
    except Exception:
        pass
    _THEME_STOCK_CACHE[theme_no] = codes
    return codes


# ── 세션 경과 비율 ──────────────────────────────────────────────────
def _session_fraction(now: datetime | None = None) -> float:
    now = now or datetime.now()
    start = now.replace(hour=_SESSION_START[0], minute=_SESSION_START[1], second=0, microsecond=0)
    elapsed = (now - start).total_seconds() / 60.0
    frac = elapsed / _SESSION_MIN
    return min(max(frac, 0.02), 1.0)  # 너무 이른 시각엔 하한 2%


# ── 4) 대장주 선별 ──────────────────────────────────────────────────
def find_leaders_by_theme(rank_df: pd.DataFrame, vol_mult: float, frac: float,
                          min_value: float = 500e8, min_mktcap: float = 1000e8,
                          max_change: float = 29.5,
                          theme_min_change: float = 3.0) -> dict:
    """테마 기반 대장주 선별.

    ① 거래대금 상위 rank_df (기존)
    ② 네이버 핫테마 목록 (등락률 theme_min_change% 이상)
    ③ 핫테마 ∩ rank_df 교집합 → 후보
    ④ 후보 중 거래대금·상승률 조건 통과한 상승률 1위 = 대장주
    """
    if rank_df.empty:
        return {"hot_sectors": [], "leaders": []}

    screen_df = rank_df[rank_df["change_pct"] < max_change].copy()
    rank_codes = set(screen_df["code"].tolist())

    # 핫테마 가져오기
    hot_themes = fetch_theme_list(min_change=theme_min_change)
    if not hot_themes:
        return {"hot_sectors": [], "leaders": []}

    hot_list = []   # _report 호환용
    leaders = []
    seen_codes: set = set()   # 같은 종목 중복 방지

    for theme in hot_themes:
        t_codes = fetch_theme_stocks(theme["no"])
        # 교집합: 거래대금 상위 + 테마
        cands = screen_df[screen_df["code"].isin(t_codes & rank_codes)]
        cands = cands.sort_values("change_pct", ascending=False)

        riser_count = int((cands["change_pct"] >= 3.0).sum())
        hot_list.append({
            "sector": theme["name"],
            "riser_count": riser_count,
            "total_value": float(cands["value_won"].sum()),
            "avg_change": theme["change_pct"],
        })

        for _, row in cands.iterrows():
            if row["code"] in seen_codes:
                continue
            if row["value_won"] < min_value:
                continue
            if "market_cap" in row.index:
                if row["market_cap"] > 0 and row["market_cap"] < min_mktcap:
                    continue
            avg5 = avg_value_5d(row["code"])
            expected = avg5 * frac if avg5 > 0 else 0
            ratio = row["value_won"] / expected if expected > 0 else 0.0
            if ratio >= vol_mult and row["change_pct"] >= 3.0:
                leaders.append({
                    "sector": theme["name"],
                    "code": row["code"], "name": row["name"],
                    "change_pct": row["change_pct"], "price": row["price"],
                    "value_won": row["value_won"], "vol_ratio": ratio,
                    "sector_risers": riser_count,
                    "theme_change": theme["change_pct"],
                })
                seen_codes.add(row["code"])
                break

    leaders.sort(key=lambda x: x["change_pct"], reverse=True)
    hot_list.sort(key=lambda x: x["avg_change"], reverse=True)
    return {"hot_sectors": hot_list, "leaders": leaders}


def find_leaders(rank_df: pd.DataFrame, rise_min: float, hot_min: int,
                 vol_mult: float, frac: float,
                 min_value: float = 500e8,
                 min_mktcap: float = 1000e8,
                 max_change: float = 29.5) -> dict:
    """반환: {hot_sectors: [...], leaders: [...] }.

    min_value   : 거래대금 최소 절대값 (원). 기본 500억.
    min_mktcap  : 시가총액 최소 (원). 기본 1000억. market_cap=0이면 통과.
    max_change  : 등락률 상한 (%). 기본 29.5% → 상한가(30%) 제외.
    """
    if rank_df.empty:
        return {"hot_sectors": [], "leaders": []}

    # 섹터 부착 (유니버스 전체 — 핫섹터 내 비상승 종목도 후보가 되므로)
    rank_df = rank_df.copy()
    rank_df["sector"] = rank_df["code"].map(sector_of)

    # ── 핫섹터 판별용: 상한가만 제외 (거래대금/시총 필터 없이 전체로 섹터 집계) ──
    screen_df = rank_df[rank_df["change_pct"] < max_change].copy()

    # 상승 종목
    risers = screen_df[screen_df["change_pct"] >= rise_min]

    # 섹터별 상승 종목 집계 (전체 기준 — 핫섹터 판별)
    sec_stats = []
    for sec, g in risers.groupby("sector"):
        if sec in ("", "(미상)"):
            continue
        sec_stats.append({
            "sector": sec,
            "riser_count": len(g),
            "total_value": float(g["value_won"].sum()),
            "avg_change": float(g["change_pct"].mean()),
        })
    # 핫섹터: 상승 종목 hot_min 개 이상, 자금유입(거래대금 합)순
    hot = [s for s in sec_stats if s["riser_count"] >= hot_min]
    hot.sort(key=lambda s: s["total_value"], reverse=True)

    # 각 핫섹터에서 대장주 선정:
    # 상승률 1위 + 거래대금 평소대비 배수 + 거래대금 절대값 + 시총 조건 모두 충족
    leaders = []
    for s in hot:
        sec = s["sector"]
        g = screen_df[screen_df["sector"] == sec].sort_values("change_pct", ascending=False)
        for _, row in g.iterrows():
            # 거래대금 절대값 필터 (대장주 후보에만 적용)
            if row["value_won"] < min_value:
                continue
            # 시가총액 필터 (0이면 데이터 없는 것으로 간주 → 통과)
            if "market_cap" in row.index:
                if row["market_cap"] > 0 and row["market_cap"] < min_mktcap:
                    continue
            avg5 = avg_value_5d(row["code"])
            if avg5 <= 0:
                ratio = 0.0
            else:
                expected = avg5 * frac
                ratio = row["value_won"] / expected if expected > 0 else 0.0
            if ratio >= vol_mult and row["change_pct"] >= rise_min:
                leaders.append({
                    "sector": sec, "code": row["code"], "name": row["name"],
                    "change_pct": row["change_pct"], "price": row["price"],
                    "value_won": row["value_won"], "vol_ratio": ratio,
                    "sector_risers": s["riser_count"],
                })
                break  # 섹터당 1위만
    leaders.sort(key=lambda x: x["change_pct"], reverse=True)
    return {"hot_sectors": hot, "leaders": leaders}


# ── 리포트 출력 ─────────────────────────────────────────────────────
def _report(rank_df: pd.DataFrame, res: dict, args, frac: float) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'='*96}")
    print(f"[{now}] 09:00~현재 누적 거래대금 상위 {len(rank_df)}종목 | 세션경과 {frac*100:.0f}% | "
          f"상승기준 +{args.rise_min:g}% | 거래대금 {args.vol_mult:g}배·{args.min_value:.0f}억↑ | "
          f"시총 {args.min_mktcap:.0f}억↑ | 상한가({args.max_change:g}%↑) 제외")
    print("=" * 96)

    hot = res["hot_sectors"]
    if hot:
        print(f"\n■ 주도(핫) 섹터  (상승종목 {args.hot_min}개+ , 자금유입순)")
        print(f"{'섹터':<20} {'상승종목수':>8} {'거래대금합(억)':>14} {'평균등락':>8}")
        print("-" * 56)
        for s in hot[:8]:
            print(f"{s['sector']:<20} {s['riser_count']:>8} "
                  f"{s['total_value']/1e8:>13,.0f} {s['avg_change']:>+7.2f}%")
    else:
        print("\n  핫섹터 없음 (상승 종목이 섹터별로 충분치 않음)")

    leaders = res["leaders"]
    print(f"\n■ 대장주 후보  (핫섹터별 상승률 1위 + 거래대금 {args.vol_mult:g}배 이상)")
    if leaders:
        print(f"{'섹터':<18} {'종목':<16} {'현재가':>9} {'등락률':>8} "
              f"{'거래대금(억)':>12} {'평소대비':>8}")
        print("-" * 80)
        for L in leaders:
            print(f"{L['sector']:<18} {L['name'][:14]:<16} {L['price']:>9,.0f} "
                  f"{L['change_pct']:>+7.2f}% {L['value_won']/1e8:>11,.0f} "
                  f"{L['vol_ratio']:>6.1f}x")
    else:
        print("  조건 충족 대장주 없음")
    print()


def _discord_notify(res: dict, args, frac: float) -> None:
    """대장주 선별 결과를 디스코드로 전송."""
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    now = datetime.now().strftime("%H:%M")
    leaders = res.get("leaders", [])
    hot = res.get("hot_sectors", [])

    lines = [f"**📊 대장주 선별 [{now}]** | 세션경과 {frac*100:.0f}%"]

    if leaders:
        lines.append("")
        lines.append("**🏆 대장주 후보**")
        for i, L in enumerate(leaders, 1):
            lines.append(
                f"`{i}위` **{L['name']}** ({L['code']})  "
                f"{L['change_pct']:+.1f}%  "
                f"거래대금 {L['value_won']/1e8:.0f}억  "
                f"평소대비 {L['vol_ratio']:.1f}x"
            )
            lines.append(f"　　　섹터: {L['sector']}")
    else:
        lines.append("⚠️ 조건 충족 대장주 없음")

    if hot:
        lines.append("")
        lines.append("**🔥 핫섹터**")
        for s in hot[:5]:
            lines.append(
                f"• {s['sector']}  상승종목 {s['riser_count']}개  "
                f"평균 {s['avg_change']:+.1f}%  "
                f"{s['total_value']/1e8:.0f}억"
            )

    msg = "\n".join(lines)
    try:
        r = requests.post(url, json={"content": msg, "username": "대장주알림"}, timeout=10)
        if not (200 <= r.status_code < 300):
            print(f"  [디스코드 전송 실패] HTTP {r.status_code}")
    except Exception as e:
        print(f"  [디스코드 전송 실패] {e}")


def _is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60) <= hm <= (15 * 60 + 30)


_PICKS_DIR = _CACHE_DIR / "leader_picks"


def _save_picks(res: dict, args, frac: float) -> None:
    """선별된 대장주를 날짜별 JSON으로 적재(전진검증용). 다음날 점수화에 사용."""
    leaders = res.get("leaders", [])
    if not leaders:
        return
    _PICKS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = _PICKS_DIR / f"{now:%Y-%m-%d}.json"
    payload = {
        "date": now.strftime("%Y-%m-%d"),
        "selected_at": now.strftime("%H:%M:%S"),
        "session_fraction": round(frac, 4),
        "params": {"rise_min": args.rise_min, "hot_min": args.hot_min,
                   "vol_mult": args.vol_mult, "top": args.top},
        "leaders": [
            {"code": L["code"], "name": L["name"], "sector": L["sector"],
             "change_pct": round(float(L["change_pct"]), 2),
             "price": float(L["price"]),
             "value_won": float(L["value_won"]),
             "vol_ratio": round(float(L["vol_ratio"]), 2)}
            for L in leaders
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"  → 선별 결과 저장: {path.relative_to(HERE)} ({len(leaders)}종목)")


def run_once(args) -> None:
    frac = _session_fraction()
    rank_df = fetch_ranking(top_n=args.top, stock_only=not args.include_etf)
    if rank_df.empty:
        print("  [경고] 순위 데이터 수집 실패")
        return
    if getattr(args, "theme", False):
        print("  [테마 모드] 네이버 테마 기반 선별")
        res = find_leaders_by_theme(rank_df, args.vol_mult, frac,
                                    min_value=args.min_value * 1e8,
                                    min_mktcap=args.min_mktcap * 1e8,
                                    max_change=args.max_change,
                                    theme_min_change=args.theme_min_change)
    else:
        res = find_leaders(rank_df, args.rise_min, args.hot_min, args.vol_mult, frac,
                           min_value=args.min_value * 1e8,
                           min_mktcap=args.min_mktcap * 1e8,
                           max_change=args.max_change)
    _report(rank_df, res, args, frac)
    _discord_notify(res, args, frac)
    _save_picks(res, args, frac)
    _save_avgval_cache()


def _wait_until(hh: int, mm: int) -> None:
    """오늘 hh:mm 까지 대기. 이미 지났으면 즉시 반환."""
    target = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    while True:
        now = datetime.now()
        if now >= target:
            return
        remain = (target - now).total_seconds()
        print(f"[{now:%H:%M:%S}] {hh:02d}:{mm:02d} 선별까지 {remain/60:.0f}분 대기…", flush=True)
        time.sleep(min(remain, 30))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", type=str, default="10:00",
                    help="선별 시각 HH:MM (이 시각까지 9시부터의 누적 거래대금으로 1회 선별)")
    ap.add_argument("--top", type=int, default=100, help="거래대금 상위 N")
    ap.add_argument("--rise-min", type=float, default=3.0, help="상승 종목 등락률 하한 %")
    ap.add_argument("--hot-min", type=int, default=3, help="핫섹터 최소 상승종목 수 (기본 3)")
    ap.add_argument("--vol-mult", type=float, default=2.0, help="거래대금 평소대비 배수 게이트")
    ap.add_argument("--min-value", type=float, default=500.0, help="거래대금 최소 절대값 (억원, 기본 500)")
    ap.add_argument("--min-mktcap", type=float, default=1000.0, help="시가총액 최소 (억원, 기본 1000)")
    ap.add_argument("--max-change", type=float, default=29.5, help="등락률 상한 % — 상한가 제외 (기본 29.5)")
    ap.add_argument("--once", action="store_true", help="대기 없이 지금 즉시 1회(테스트)")
    ap.add_argument("--include-etf", action="store_true", help="ETF/ETN 포함(기본 제외)")
    ap.add_argument("--ignore-hours", action="store_true", help="장시간 무시하고 실행")
    ap.add_argument("--theme", action="store_true", help="테마 기반 선별 모드 (기본: 업종 기반)")
    ap.add_argument("--theme-min-change", type=float, default=3.0,
                    help="테마 모드: 핫테마 최소 등락률 %% (기본 3.0)")
    args = ap.parse_args()

    _load_avgval_cache()
    print(f"대장주 탐색기 | 상위{args.top} 상승+{args.rise_min:g}% "
          f"핫섹터{args.hot_min}+ 거래대금{args.vol_mult:g}배 | "
          f"{'즉시1회' if args.once else f'{args.at} 선별'}")

    if args.once:
        if not args.ignore_hours and not _is_market_hours():
            print("  (장시간 아님 — 최신 순위로 즉시 1회 실행)")
        run_once(args)
        return

    # 지정 시각까지 대기 후 1회 선별 (9시부터의 누적 거래대금이 그 시점에 반영됨)
    try:
        hh, mm = (int(x) for x in args.at.split(":"))
    except Exception:
        print(f"  [오류] --at 형식은 HH:MM 이어야 함 (입력: {args.at})")
        return
    if not args.ignore_hours and datetime.now().weekday() >= 5:
        print("  주말 — 선별 생략(테스트는 --once --ignore-hours)")
        return
    _wait_until(hh, mm)
    run_once(args)


if __name__ == "__main__":
    main()
