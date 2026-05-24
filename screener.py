"""종목 스크리너 — 주간/일간 2단계 종목 자동 선별.

[주간 — 매주 월요일 8:00]
  전체 후보(screener_candidates.txt) → 재무+기술적 종합 점수
  → 상위 12개를 screener_weekly_pool.txt에 저장

[일간 — 매일(월~금) 8:30]
  screener_weekly_pool.txt 12개 → 기술적 분석만 (빠름)
  → 당일 상위 6개 → .env.overrides SYMBOLS 업데이트
  → git commit+push → 파이 서버 자동 반영

사용:
  python screener.py --mode weekly   # 주간 풀 갱신 (재무+기술, ~3분)
  python screener.py --mode daily    # 일간 선별 (기술적만, ~30초)
  python screener.py --mode weekly --dry-run   # 업데이트 없이 점수만 출력
  python screener.py --mode daily  --dry-run
  python screener.py --mode weekly --top 12 --pool-top 15
"""
from __future__ import annotations
import io, os, sys, re, time, tempfile, shutil, argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import certifi
    _cert_dst = os.path.join(tempfile.gettempdir(), "cacert.pem")
    if not os.path.exists(_cert_dst):
        shutil.copy(certifi.where(), _cert_dst)
    os.environ.setdefault("CURL_CA_BUNDLE", _cert_dst)
    os.environ.setdefault("SSL_CERT_FILE", _cert_dst)
except Exception:
    pass

import logging
import pandas as pd
import numpy as np
import yfinance as yf

# yfinance 내부 401/429 에러 로그 억제 (정상적으로 예외 처리됨)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)

# ── yfinance 401/429 rate-limit 재시도 헬퍼 ──────────────────────────
_YF_RETRY_DELAYS = (2, 5, 10)  # 최대 3회 재시도 대기 시간(초)

def _yf_download(sym: str, **kwargs) -> pd.DataFrame:
    """yf.download 래퍼 — 401/429 시 최대 3회 재시도."""
    for attempt, delay in enumerate(_YF_RETRY_DELAYS, 1):
        try:
            return yf.download(sym, **kwargs)
        except Exception as e:
            msg = str(e)
            if "401" in msg or "429" in msg or "Unauthorized" in msg or "Too Many" in msg:
                time.sleep(delay)
            else:
                raise
    return yf.download(sym, **kwargs)  # 마지막 시도 (예외 그대로 전파)


def _yf_ticker_info(sym: str):
    """yf.Ticker(sym).info 래퍼 — 401/429 시 최대 3회 재시도."""
    for attempt, delay in enumerate(_YF_RETRY_DELAYS, 1):
        try:
            return yf.Ticker(sym).info
        except Exception as e:
            msg = str(e)
            if "401" in msg or "429" in msg or "Unauthorized" in msg or "Too Many" in msg:
                time.sleep(delay)
            else:
                raise
    return yf.Ticker(sym).info


def _yf_earnings_history(sym: str):
    """yf.Ticker(sym).earnings_history 래퍼 — 401/429 시 최대 3회 재시도."""
    for attempt, delay in enumerate(_YF_RETRY_DELAYS, 1):
        try:
            return yf.Ticker(sym).earnings_history
        except Exception as e:
            msg = str(e)
            if "401" in msg or "429" in msg or "Unauthorized" in msg or "Too Many" in msg:
                time.sleep(delay)
            else:
                raise
    return yf.Ticker(sym).earnings_history

HERE = Path(__file__).parent
CANDIDATES_FILE  = HERE / "screener_candidates.txt"
WEEKLY_POOL_FILE = HERE / "screener_weekly_pool.txt"

# ══════════════════════════════════════════════════════════════════
#  DART(전자공시) 재무 데이터 헬퍼
# ══════════════════════════════════════════════════════════════════
_DART_CLIENT   = None
_DART_CORP_DF  = None   # corp_codes DataFrame 캐시 (최초 1회 다운로드)
_DART_FIN_CACHE: dict[str, dict] = {}  # stock_code → 재무지표 캐시


def _get_dart():
    """OpenDartReader 클라이언트 (싱글턴)."""
    global _DART_CLIENT
    if _DART_CLIENT is None:
        try:
            import OpenDartReader as _odr
            api_key = os.environ.get("DART_API_KEY", "")
            if not api_key:
                raise RuntimeError("DART_API_KEY 환경 변수 없음")
            _DART_CLIENT = _odr.OpenDartReader(api_key)
        except ImportError:
            raise RuntimeError("opendartreader 미설치")
    return _DART_CLIENT


def _dart_corp_code(stock_code: str) -> str:
    """6자리 주식코드 → DART 8자리 corp_code. 실패 시 빈 문자열."""
    global _DART_CORP_DF
    try:
        dart = _get_dart()
        if _DART_CORP_DF is None:
            _DART_CORP_DF = dart.corp_codes  # ZIP 다운로드 (최초 1회, 이후 캐시)
        rows = _DART_CORP_DF[_DART_CORP_DF["stock_code"] == stock_code]
        return rows["corp_code"].values[0] if not rows.empty else ""
    except Exception:
        return ""


def _dart_financials(stock_code: str) -> dict:
    """DART 연간 재무제표에서 핵심 지표 추출.

    반환 키: revenueGrowth, earningsGrowth, returnOnEquity, debtToEquity
    """
    if stock_code in _DART_FIN_CACHE:
        return _DART_FIN_CACHE[stock_code]

    result: dict = {}
    try:
        dart = _get_dart()
        corp_code = _dart_corp_code(stock_code)
        if not corp_code:
            return result

        cur_year = datetime.now().year
        fs = None
        # 작년 → 재작년 순으로 연간 사업보고서 시도 (연결 우선, 없으면 별도)
        for y in [cur_year - 1, cur_year - 2]:
            for div in ["CFS", "OFS"]:
                try:
                    tmp = dart.finstate(corp_code, y, "11011", fs_div=div)
                    if tmp is not None and not (isinstance(tmp, pd.DataFrame) and tmp.empty):
                        fs = tmp if isinstance(tmp, pd.DataFrame) else pd.DataFrame(tmp)
                        if not fs.empty:
                            break
                except Exception:
                    continue
            if fs is not None and not fs.empty:
                break

        if fs is None or fs.empty:
            _DART_FIN_CACHE[stock_code] = result
            return result

        def _get(keyword: str, col: str = "thstrm_amount") -> float | None:
            rows = fs[fs["account_nm"].str.contains(keyword, na=False, regex=False)]
            if rows.empty:
                return None
            raw = rows.iloc[0][col]
            try:
                v = float(str(raw).replace(",", "").replace(" ", ""))
                return v if v != 0 else None
            except Exception:
                return None

        rev_cur = _get("매출액")
        rev_prv = _get("매출액",       "frmtrm_amount")
        inc_cur = _get("당기순이익")
        inc_prv = _get("당기순이익",   "frmtrm_amount")
        equity  = _get("자본총계")
        debt    = _get("부채총계")

        if rev_cur and rev_prv:
            result["revenueGrowth"]  = (rev_cur - rev_prv) / abs(rev_prv)
        if inc_cur and inc_prv:
            result["earningsGrowth"] = (inc_cur - inc_prv) / abs(inc_prv)
        if inc_cur and equity:
            result["returnOnEquity"] = inc_cur / abs(equity)
        if debt is not None and equity:
            result["debtToEquity"]   = (debt / abs(equity)) * 100

    except Exception:
        pass

    _DART_FIN_CACHE[stock_code] = result
    return result

# ── GICS 섹터 한/영 매핑 (광범위) ────────────────────────────────────
SECTOR_MAP: dict[str, str] = {
    "Technology":             "IT",
    "Financial Services":     "금융",
    "Industrials":            "산업재",
    "Consumer Defensive":     "필수소비재",
    "Consumer Cyclical":      "경기소비재",
    "Healthcare":             "헬스케어",
    "Basic Materials":        "소재",
    "Energy":                 "에너지",
    "Communication Services": "통신서비스",
    "Real Estate":            "부동산",
    "Utilities":              "유틸리티",
}
# 역방향 (한글 → 영문)
_SECTOR_MAP_REV: dict[str, str] = {v: k for k, v in SECTOR_MAP.items()}

# ── 세부 산업(industry) 한/영 매핑 ────────────────────────────────────
INDUSTRY_MAP: dict[str, str] = {
    # 반도체/전자
    "Semiconductors":                            "반도체",
    "Semiconductor Equipment & Materials":       "반도체장비",
    "Electronic Components":                     "전자부품",
    "Consumer Electronics":                      "가전/전자",
    # IT 서비스/소프트웨어
    "Internet Content & Information":            "인터넷/플랫폼",
    "Software—Application":                      "소프트웨어",
    "Software—Infrastructure":                   "소프트웨어",
    "IT Services":                               "IT서비스",
    "Information Technology Services":           "IT서비스",
    # 자동차
    "Auto Manufacturers":                        "자동차",
    "Auto Parts":                                "자동차부품",
    # 배터리/전기
    "Electrical Equipment & Parts":              "배터리/전기",
    "Specialty Chemicals":                       "화학",
    "Chemicals":                                 "화학",
    # 금융
    "Banks—Diversified":                         "은행",
    "Banks—Regional":                            "은행",
    "Capital Markets":                           "증권",
    "Insurance—Life":                            "보험",
    "Insurance—Diversified":                     "보험",
    "Insurance—Property & Casualty":             "보험",
    # 바이오/제약
    "Biotechnology":                             "바이오",
    "Drug Manufacturers—General":                "제약",
    "Drug Manufacturers—Specialty & Generic":    "제약",
    # 소재/에너지
    "Steel":                                     "철강",
    "Aluminum":                                  "비철금속",
    "Oil & Gas Integrated":                      "정유",
    "Oil & Gas Refining & Marketing":            "정유",
    # 통신
    "Telecom Services":                          "통신",
    # 산업재
    "Engineering & Construction":                "건설",
    "Specialty Industrial Machinery":            "기계/중공업",
    "Industrial Machinery":                      "기계/중공업",
    "Aerospace & Defense":                       "방산/항공",
    "Diversified Industrials":                   "복합산업",
    "Marine Shipping":                           "조선/해운",
    "Shipping & Ports":                          "조선/해운",
}
# 역방향 (한글 → 영문 키 목록)
_INDUSTRY_MAP_REV: dict[str, list[str]] = {}
for _eng, _kor in INDUSTRY_MAP.items():
    _INDUSTRY_MAP_REV.setdefault(_kor, []).append(_eng)
OVERRIDES_FILE   = HERE / ".env.overrides"

# ── 점수 가중치 (총점 정규화: tech/10 * W_TECH + fund/15 * W_FUND) ──
W_TECH  = 0.50   # 기술적 분석 비중
W_FUND  = 0.50   # 재무제표 비중 (실적 서프라이즈 추가로 상향)


# ══════════════════════════════════════════════════════════════════
#  후보 종목 로드
# ══════════════════════════════════════════════════════════════════
def load_candidates() -> list[str]:
    symbols = []
    with open(CANDIDATES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                symbols.append(line)
    return symbols


# ── 시가총액 기준 KOSPI/KOSDAQ 상위 종목 (pykrx 인증 실패 시 폴백) ────────
_FALLBACK_KOSPI = [
    "005930.KS","000660.KS","207940.KS","005380.KS","000270.KS",
    "373220.KS","068270.KS","105560.KS","055550.KS","028260.KS",
    "012330.KS","066570.KS","032830.KS","003550.KS","086790.KS",
    "323410.KS","030200.KS","015760.KS","034020.KS","010950.KS",
    "003490.KS","035420.KS","035720.KS","000810.KS","329180.KS",
    "316140.KS","024110.KS","009150.KS","011200.KS","096770.KS",
    "033780.KS","000720.KS","047050.KS","005490.KS","018260.KS",
    "034730.KS","010130.KS","017670.KS","010140.KS","090430.KS",
    "051910.KS","006400.KS","009830.KS","047810.KS","032640.KS",
    "402340.KS","010120.KS","298040.KS","267260.KS","006800.KS",
    "042700.KS","012450.KS","042660.KS","009540.KS","011070.KS",
    "021240.KS","069960.KS","000100.KS","139480.KS","002790.KS",
    "003670.KS","015020.KS","008770.KS","000150.KS","001570.KS",
    "006360.KS","161890.KS","009240.KS","271560.KS","078930.KS",
    "001040.KS","003410.KS","000080.KS","004370.KS","007070.KS",
    "005830.KS","016360.KS","010060.KS","002380.KS","004020.KS",
    "001800.KS","006280.KS","007310.KS","000120.KS","014680.KS",
    "003230.KS","007340.KS","011780.KS","005070.KS","001680.KS",
    "001530.KS","004990.KS","009200.KS","003030.KS","008490.KS",
]
_FALLBACK_KOSDAQ = [
    "247540.KQ","086520.KQ","091990.KQ","196170.KQ","145020.KQ",
    "263750.KQ","112040.KQ","357780.KQ","041510.KQ","036830.KQ",
    "067160.KQ","058470.KQ","028300.KQ","046080.KQ","214150.KQ",
    "018290.KQ","054040.KQ","039030.KQ","095340.KQ","065350.KQ",
]


def load_kospi_all(market: str = "kospi", top_n: int = 0) -> list[str]:
    """pykrx로 코스피/코스닥 종목 목록을 가져온다. 실패 시 하드코딩 폴백.

    market: kospi | kosdaq | all
    top_n: 상위 N개만 (0=전체)
    부수 효과: SYM_NAMES 글로벌 딕셔너리에 회사명 추가
    """
    from pykrx import stock as _krx
    from datetime import datetime, timedelta

    # 가장 최근 영업일 계산 (주말이면 금요일로)
    d = datetime.now()
    for _ in range(7):
        if d.weekday() < 5:
            break
        d -= timedelta(days=1)

    def _fetch_tickers(mkt: str, date_str: str) -> list[str]:
        """인증 불필요한 get_market_ticker_list로 종목 코드 목록 반환."""
        for _ in range(3):
            try:
                tickers = _krx.get_market_ticker_list(date_str, market=mkt)
                if tickers:
                    return list(tickers)
            except Exception:
                pass
            # 하루 전 재시도
            d2 = datetime.strptime(date_str, "%Y%m%d") - timedelta(days=1)
            while d2.weekday() >= 5:
                d2 -= timedelta(days=1)
            date_str = d2.strftime("%Y%m%d")
        return []

    date_str = d.strftime("%Y%m%d")
    all_codes: list[tuple[str, str]] = []  # (code, suffix)
    for mkt, suffix in [("KOSPI", ".KS"), ("KOSDAQ", ".KQ")]:
        if market not in (mkt.lower(), "all"):
            continue
        codes = _fetch_tickers(mkt, date_str)
        if not codes:
            print(f"  [{mkt}] 종목 목록 로딩 실패")
            continue
        # 보통주만 (마지막 자리 0)
        codes = [c for c in codes if re.match(r"^\d{5}0$", c)]
        all_codes.extend((c, suffix) for c in codes)

    if not all_codes:
        # ── pykrx 실패 → 하드코딩 폴백 ──────────────────────────────
        print(f"  [경고] pykrx 종목 목록 실패 → 내장 폴백 목록 사용")
        fallback: list[str] = []
        if market in ("kospi", "all"):
            fallback.extend(_FALLBACK_KOSPI)
        if market in ("kosdaq", "all"):
            fallback.extend(_FALLBACK_KOSDAQ)
        if top_n > 0:
            fallback = fallback[:top_n]
        # SYM_NAMES 등록 (이미 있는 것만)
        for ticker in fallback:
            _ = SYM_NAMES.get(ticker)
        return fallback

    # top_n 제한 (시가총액 정렬 없이 앞에서 자름 — KRX 목록이 대체로 시총순)
    if top_n > 0:
        all_codes = all_codes[:top_n]

    result = []
    for code, suffix in all_codes:
        ticker = code + suffix
        result.append(ticker)
        if ticker not in SYM_NAMES:
            try:
                SYM_NAMES[ticker] = _krx.get_market_ticker_name(code)
            except Exception:
                pass

    return result


def load_weekly_pool() -> list[str]:
    if not WEEKLY_POOL_FILE.exists():
        print("[!] screener_weekly_pool.txt 없음 — weekly 먼저 실행하세요")
        return []
    symbols = []
    with open(WEEKLY_POOL_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                symbols.append(line)
    return symbols


def save_weekly_pool(symbols: list[str]):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# 주간 스크리너 풀 — {ts}\n"]
    lines += [f"{s}\n" for s in symbols]
    WEEKLY_POOL_FILE.write_text("".join(lines), encoding="utf-8")
    print(f"\n[완료] 주간 풀 저장: {WEEKLY_POOL_FILE.name}  ({len(symbols)}개)")


# ══════════════════════════════════════════════════════════════════
#  기술적 분석 점수 (0 ~ 10)
# ══════════════════════════════════════════════════════════════════
def tech_score(sym: str) -> tuple[float, dict]:
    """일봉 60일 데이터로 기술적 점수 계산."""
    detail = {}
    try:
        df = _yf_download(sym, period="90d", interval="1d",
                          auto_adjust=True, progress=False)
        if df.empty or len(df) < 20:
            return 0.0, {"error": "no data"}
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                      for c in df.columns]
        close = df["close"]
        volume = df["volume"]

        score = 0.0

        # 1) 20일 SMA 위 (+2)
        sma20 = close.rolling(20).mean().iloc[-1]
        cur   = float(close.iloc[-1])
        above_sma20 = cur > float(sma20)
        if above_sma20:
            score += 2
        detail["SMA20"] = f"{'위' if above_sma20 else '아래'} ({cur:.0f} vs {sma20:.0f})"

        # 2) 60일 SMA 위 (+2)
        if len(close) >= 60:
            sma60 = close.rolling(60).mean().iloc[-1]
            above_sma60 = cur > float(sma60)
            if above_sma60:
                score += 2
            detail["SMA60"] = f"{'위' if above_sma60 else '아래'} ({sma60:.0f})"
        else:
            detail["SMA60"] = "데이터부족"

        # 3) RSI (+2) — 강한 추세 종목 패널티 없앰
        #   45~72: 건강한 상승 (+2)
        #   72~82: 강한 추세 (약간 과열이지만 유효) (+1.5)
        #   >82  : 매우 강한 모멘텀 (단기 피로 가능, but 추세 자체는 유효) (+1.0)
        #   <45  : 추세 없음 (0)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])
        if 45 <= rsi <= 72:
            score += 2
        elif 72 < rsi <= 82:
            score += 1.5   # 강한 추세 — 부분 점수
        elif rsi > 82:
            score += 1.0   # 매우 강한 모멘텀 — 최소 점수 보장
        detail["RSI14"] = f"{rsi:.1f}"

        # 4) 20일 수익률 (+2) — 모멘텀 강도 반영
        ret20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0.0
        if ret20 > 10:
            score += 2      # 강한 상승 모멘텀
        elif ret20 > 0:
            score += 1
        detail["ROC20"] = f"{ret20:+.1f}%"

        # 5) 거래량 증가: 5일 평균 > 20일 평균 (+1)
        vol5  = float(volume.iloc[-5:].mean())
        vol20 = float(volume.iloc[-20:].mean())
        vol_surge = vol5 > vol20 * 1.1
        if vol_surge:
            score += 1
        detail["거래량"] = f"{'증가' if vol_surge else '보통'} (5일평균 {vol5/vol20:.2f}x)"

        # 6) 52주 고점 대비 위치 — 80% 이상 (+1)
        high52 = float(close.rolling(min(len(close), 252)).max().iloc[-1])
        pos52  = cur / high52 * 100
        if pos52 >= 80:
            score += 1
        detail["52주고점"] = f"{pos52:.0f}%"

        # 7) Supertrend 방향 (+1) — 봇 핵심 지표, 상승방향이면 가산
        try:
            high_s = df["high"]
            low_s  = df["low"]
            # ATR(7)
            hl  = high_s - low_s
            hpc = (high_s - close.shift(1)).abs()
            lpc = (low_s  - close.shift(1)).abs()
            tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
            atr = tr.rolling(7).mean()
            mult = 3.0
            ub = ((high_s + low_s) / 2 + mult * atr).ffill()
            lb = ((high_s + low_s) / 2 - mult * atr).ffill()
            # 간단 Supertrend 방향 (마지막 10봉 기준)
            st_dir = 1
            for i in range(-10, 0):
                c = float(close.iloc[i])
                if st_dir == 1:
                    st_dir = -1 if c < float(lb.iloc[i]) else 1
                else:
                    st_dir = 1  if c > float(ub.iloc[i]) else -1
            if st_dir == 1:
                score += 1
            detail["Supertrend"] = "상승" if st_dir == 1 else "하락"
        except Exception:
            detail["Supertrend"] = "N/A"

        return min(score, 10.0), detail

    except Exception as e:
        return 0.0, {"error": str(e)[:60]}


# ══════════════════════════════════════════════════════════════════
#  재무제표 점수 (0 ~ 15)
# ══════════════════════════════════════════════════════════════════
def fundamental_score(sym: str) -> tuple[float, dict]:
    """DART(전자공시) + yfinance 혼합으로 재무 점수 계산.

    DART (안정적, 한국 공식 데이터):
      ROE, 매출성장, 이익성장, 부채비율 (4개 지표)

    yfinance (보조):
      ForwardPE, 섹터/산업 분류, 실적 서프라이즈

    기존 항목 (10점):
      PER, ROE, 매출성장, 이익성장, 부채비율

    신규 항목 (5점):
      실적 서프라이즈 최근치, 연속 어닝비트 횟수, EPS 성장 추세
    """
    detail = {}
    score  = 0.0
    stock_code = sym.split(".")[0]  # "005930.KS" → "005930"

    # ── yfinance: 섹터/PER (실패해도 계속) ──────────────────────────
    info: dict = {}
    try:
        info = _yf_ticker_info(sym)
    except Exception:
        pass

    raw_sector   = info.get("sector",   "")
    raw_industry = info.get("industry", "")
    detail["sector_en"]   = raw_sector
    detail["sector"]      = SECTOR_MAP.get(raw_sector, raw_sector)
    detail["industry_en"] = raw_industry
    detail["industry"]    = INDUSTRY_MAP.get(raw_industry, raw_industry)

    # 1) Forward PER (+3) — yfinance (DART에 없음)
    fpe = info.get("forwardPE") or info.get("trailingPE")
    if fpe is not None:
        try:
            fpe = float(fpe)
            if 5 <= fpe <= 25:
                score += 3
            elif 25 < fpe <= 40:
                score += 1
            detail["PER"] = f"{fpe:.1f}"
        except Exception:
            detail["PER"] = "N/A"
    else:
        detail["PER"] = "N/A"

    # ── DART: 핵심 재무지표 (401 에러 없음) ──────────────────────────
    dart = {}
    try:
        dart = _dart_financials(stock_code)
    except Exception:
        pass

    # 2) ROE (+2) — DART 우선, 없으면 yfinance
    roe = dart.get("returnOnEquity") or info.get("returnOnEquity")
    if roe is not None:
        if roe > 0.15:
            score += 2
        elif roe > 0.05:
            score += 1
        detail["ROE"] = f"{roe*100:.1f}%"
    else:
        detail["ROE"] = "N/A"

    # 3) 매출성장 (+2) — DART 우선
    rev_g = dart.get("revenueGrowth") or info.get("revenueGrowth")
    if rev_g is not None:
        if rev_g > 0.05:
            score += 2
        elif rev_g > 0:
            score += 1
        detail["매출성장"] = f"{rev_g*100:+.1f}%"
    else:
        detail["매출성장"] = "N/A"

    # 4) 이익성장 (+2) — DART 우선
    earn_g = dart.get("earningsGrowth") or info.get("earningsGrowth")
    if earn_g is not None:
        if earn_g > 0.1:
            score += 2
        elif earn_g > 0:
            score += 1
        detail["이익성장"] = f"{earn_g*100:+.1f}%"
    else:
        detail["이익성장"] = "N/A"

    # 5) 부채비율 (+1) — DART 우선
    dte = dart.get("debtToEquity") or info.get("debtToEquity")
    if dte is not None:
        if dte < 50:
            score += 1
        elif dte < 150:
            score += 0.5
        detail["부채비율"] = f"{dte:.0f}%"
    else:
        detail["부채비율"] = "N/A"

    # ── 실적 서프라이즈 관련 (yfinance — 실패해도 계속) ──────────────
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            eh = _yf_earnings_history(sym)
        if eh is not None and not eh.empty and "surprisePercent" in eh.columns:
            eh = eh.sort_index(ascending=False)
            surprises = eh["surprisePercent"].dropna().tolist()

            # 6) 최근 분기 실적 서프라이즈 크기 (+2)
            if surprises:
                latest = surprises[0]
                if latest > 0.20:
                    score += 2
                    detail["최근서프라이즈"] = f"+{latest*100:.0f}% (강)"
                elif latest > 0.05:
                    score += 1.5
                    detail["최근서프라이즈"] = f"+{latest*100:.0f}%"
                elif latest > 0:
                    score += 0.5
                    detail["최근서프라이즈"] = f"+{latest*100:.0f}% (소)"
                else:
                    detail["최근서프라이즈"] = f"{latest*100:.0f}% (미달)"
            else:
                detail["최근서프라이즈"] = "N/A"

            # 7) 연속 어닝비트 횟수 (+2)
            beat_count = sum(1 for s in surprises if s > 0)
            total      = len(surprises)
            if total >= 3 and beat_count == total:
                score += 2
                detail["연속어닝비트"] = f"{beat_count}/{total}분기 전부"
            elif total >= 2 and beat_count >= total * 0.75:
                score += 1
                detail["연속어닝비트"] = f"{beat_count}/{total}분기"
            else:
                detail["연속어닝비트"] = f"{beat_count}/{total}분기"

            # 8) EPS 성장 추세 (+1)
            eps_vals = eh["epsActual"].dropna().tolist()
            if len(eps_vals) >= 3:
                recent = list(reversed(eps_vals[:4]))
                rising = all(recent[i] < recent[i+1] for i in range(len(recent)-1))
                if rising and recent[-1] > 0:
                    score += 1
                    detail["EPS추세"] = f"4분기 연속상승 ({recent[0]:.0f}→{recent[-1]:.0f})"
                else:
                    if eps_vals[1] > 0 and eps_vals[0] > eps_vals[1]:
                        score += 0.5
                        detail["EPS추세"] = "최근2분기 상승"
                    else:
                        detail["EPS추세"] = "불규칙"
            else:
                detail["EPS추세"] = "데이터부족"
        else:
            detail["최근서프라이즈"] = "N/A"
            detail["연속어닝비트"]   = "N/A"
            detail["EPS추세"]        = "N/A"
    except Exception:
        detail["최근서프라이즈"] = "N/A"
        detail["연속어닝비트"]   = "N/A"
        detail["EPS추세"]        = "N/A"

    return min(score, 15.0), detail


# ══════════════════════════════════════════════════════════════════
#  .env.overrides SYMBOLS 업데이트
# ══════════════════════════════════════════════════════════════════
def update_symbols(symbols: list[str], dry_run: bool):
    if not OVERRIDES_FILE.exists():
        print(f"\n[!] {OVERRIDES_FILE} 없음 — 업데이트 스킵")
        return

    content = OVERRIDES_FILE.read_text(encoding="utf-8")
    sym_str  = ",".join(symbols)
    new_line = f"SYMBOLS={sym_str}"

    if re.search(r"^SYMBOLS=", content, re.MULTILINE):
        new_content = re.sub(r"^SYMBOLS=.*$", new_line, content, flags=re.MULTILINE)
    else:
        new_content = content.rstrip() + f"\n{new_line}\n"

    if dry_run:
        print(f"\n[DRY-RUN] SYMBOLS 업데이트 예정:\n  {sym_str}")
    else:
        OVERRIDES_FILE.write_text(new_content, encoding="utf-8")
        print(f"\n[완료] .env.overrides SYMBOLS 업데이트:\n  {sym_str}")


SYM_NAMES = {
    "005930.KS":"삼성전자","000660.KS":"SK하이닉스","006400.KS":"삼성SDI",
    "009150.KS":"삼성전기","066570.KS":"LG전자","035720.KS":"카카오",
    "035420.KS":"NAVER","068270.KS":"셀트리온","207940.KS":"삼성바이오",
    "128940.KS":"한미약품","000100.KS":"유한양행","185750.KS":"종근당",
    "005380.KS":"현대차","000270.KS":"기아","051910.KS":"LG화학",
    "373220.KS":"LG에너지솔","247540.KS":"에코프로비엠","086520.KS":"에코프로",
    "105560.KS":"KB금융","055550.KS":"신한지주","086790.KS":"하나금융",
    "316140.KS":"우리금융","030200.KS":"KT","017670.KS":"SK텔레콤",
    "015760.KS":"한국전력","005490.KS":"POSCO홀딩스","011070.KS":"LG이노텍",
    "010950.KS":"S-Oil","000720.KS":"현대건설","009540.KS":"HD한국조선해양",
    "329180.KS":"HD현대중공업",
}


def _analyze_one(sym: str, use_fundamental: bool) -> dict:
    """단일 종목 분석 — 병렬 워커."""
    t_score, t_detail = tech_score(sym)
    if use_fundamental:
        f_score, f_detail = fundamental_score(sym)
        total = (t_score / 10.0) * W_TECH * 10 + (f_score / 15.0) * W_FUND * 10
    else:
        f_score, f_detail = 0.0, {}
        total = t_score
    sector   = f_detail.get("sector",   "") if use_fundamental else ""
    industry = f_detail.get("industry", "") if use_fundamental else ""
    return {"sym": sym, "total": total, "tech": t_score,
            "fund": f_score, "t_detail": t_detail, "f_detail": f_detail,
            "sector": sector, "industry": industry}


def _parse_sectors(sector_arg: str) -> set[str]:
    """'반도체,은행,Technology' → 매칭에 쓸 한/영 키 집합 반환.

    광범위 섹터(IT, 금융)와 세부 산업(반도체, 전자부품) 모두 허용.
    """
    if not sector_arg:
        return set()
    result = set()
    for s in sector_arg.split(","):
        s = s.strip()
        if not s:
            continue
        result.add(s)
        # 광범위 섹터: 한글↔영문 쌍방 추가
        if s in _SECTOR_MAP_REV:
            result.add(_SECTOR_MAP_REV[s])
        if s in SECTOR_MAP:
            result.add(SECTOR_MAP[s])
        # 세부 산업: 한글 → 영문 목록 추가
        for eng in _INDUSTRY_MAP_REV.get(s, []):
            result.add(eng)
        # 영문 세부 산업 → 한글 추가
        if s in INDUSTRY_MAP:
            result.add(INDUSTRY_MAP[s])
    return result


def _score_symbols(candidates, use_fundamental, top_n, label_top,
                   workers: int = 8, sector_filter: set | None = None):
    results = []
    done = 0
    total_syms = len(candidates)

    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(_analyze_one, sym, use_fundamental): sym
                   for sym in candidates}
        for fut in as_completed(futures):
            done += 1
            sym = futures[fut]
            print(f"\r  분석 중... {done}/{total_syms}  {sym:<15}", end="", flush=True)
            try:
                results.append(fut.result())
            except Exception:
                pass
    print(f"\r  분석 완료!{' '*35}")

    # 섹터/산업 필터 적용
    if sector_filter:
        before = len(results)
        results = [r for r in results
                   if r.get("sector",   "") in sector_filter
                   or r.get("industry", "") in sector_filter
                   or r.get("f_detail", {}).get("sector_en",   "") in sector_filter
                   or r.get("f_detail", {}).get("industry_en", "") in sector_filter]
        # 출력용 라벨: 한글만 표시
        labels = [s for s in sector_filter
                  if s in SECTOR_MAP.values() or s in INDUSTRY_MAP.values()]
        print(f"  산업 필터: {before}개 → {len(results)}개 ({', '.join(labels)})")

    results.sort(key=lambda x: x["total"], reverse=True)

    print(f"\n{'─'*70}")
    hdr_fund = f"{'재무':>6}" if use_fundamental else "     "
    print(f"  {'순위':<4} {'종목':<14} {'이름':<12} {'총점':>6}  {'기술':>6}  {hdr_fund}  {'섹터'}")
    print(f"{'─'*70}")
    selected = []
    for rank, r in enumerate(results, 1):
        sym  = r["sym"]
        name = SYM_NAMES.get(sym, sym[:8])
        marker = "★" if rank <= label_top else " "
        if rank <= label_top:
            selected.append(sym)
        fund_str = f"{r['fund']:>5.1f}" if use_fundamental else "     "
        industry_str = r.get("industry", "") or r.get("sector", "")
        print(f"  {marker}{rank:<3} {sym:<14} {name:<12} {r['total']:>5.1f}  {r['tech']:>5.1f}  {fund_str}  {industry_str}")
        if rank <= label_top:
            if r["t_detail"] and "error" not in r["t_detail"]:
                td = r["t_detail"]
                print(f"       기술: SMA20={td.get('SMA20','-')}  RSI={td.get('RSI14','-')}  "
                      f"ROC20={td.get('ROC20','-')}  거래량={td.get('거래량','-')}")
            if use_fundamental and r["f_detail"] and "error" not in r["f_detail"]:
                fd = r["f_detail"]
                print(f"       재무: PER={fd.get('forwardPE','-')}  ROE={fd.get('ROE','-')}  "
                      f"매출성장={fd.get('매출성장','-')}  이익성장={fd.get('이익성장','-')}")
                print(f"       실적: 서프라이즈={fd.get('최근서프라이즈','-')}  "
                      f"연속비트={fd.get('연속어닝비트','-')}  EPS추세={fd.get('EPS추세','-')}")
    print(f"{'─'*70}")
    return selected


# ══════════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",      choices=["daily","weekly"], required=True,
                        help="weekly=재무+기술(주1회), daily=기술적만(매일)")
    parser.add_argument("--dry-run",   action="store_true", help="업데이트 없이 점수만 출력")
    parser.add_argument("--top",       type=int, default=None, help="선별 종목 수")
    parser.add_argument("--pool-top",  type=int, default=12,   help="weekly 풀 크기 (기본 12)")
    parser.add_argument("--market",     choices=["file","kospi","kosdaq","all"], default="file",
                        help="file=candidates.txt, kospi=코스피전체, kosdaq=코스닥전체, all=전체")
    parser.add_argument("--market-top", type=int, default=0,
                        help="시총 상위 N개만 사용 (예: 200 → 코스피200, 0=전체)")
    parser.add_argument("--sector",    type=str, default="",
                        help="섹터 필터 (콤마 구분, 한/영 모두 가능). 예: IT,금융  또는  Technology,Industrials")
    parser.add_argument("--workers",   type=int, default=8,    help="병렬 워커 수 (기본 8)")
    args = parser.parse_args()

    print(f"\n{'='*70}")
    print(f"  종목 스크리너  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]  모드: {args.mode.upper()}")
    print(f"{'='*70}")

    if args.mode == "weekly":
        top_n = args.top or args.pool_top
        if args.market == "file":
            candidates = load_candidates()
        else:
            market_top = args.market_top
            top_label  = f"상위{market_top}" if market_top > 0 else "전체"
            print(f"  {args.market} {top_label} 종목 목록 로딩 중...", end=" ", flush=True)
            candidates = load_kospi_all(args.market, top_n=market_top)
            print(f"{len(candidates)}개")
        sector_filter = _parse_sectors(args.sector) if args.sector else None
        if sector_filter:
            korean = [s for s in sector_filter if s in SECTOR_MAP.values()]
            print(f"  섹터 필터 적용: {', '.join(korean or sector_filter)}")
        print(f"  후보 {len(candidates)}개 → 재무+기술 분석(병렬 {args.workers}개) → 상위 {top_n}개 저장\n")
        selected = _score_symbols(candidates, use_fundamental=True, top_n=top_n,
                                  label_top=top_n, workers=args.workers,
                                  sector_filter=sector_filter)
        print(f"\n  주간 풀 {top_n}개: {', '.join(selected)}")
        if not args.dry_run:
            save_weekly_pool(selected)
        else:
            print("  [DRY-RUN] 저장 스킵")

    else:  # daily
        top_n     = args.top or 6
        candidates = load_weekly_pool()
        if not candidates:
            return
        sector_filter = _parse_sectors(args.sector) if args.sector else None
        print(f"  주간 풀 {len(candidates)}개 → 기술적 분석 → 상위 {top_n}개 SYMBOLS 선별\n")
        selected = _score_symbols(candidates, use_fundamental=False, top_n=top_n,
                                  label_top=top_n, sector_filter=sector_filter)
        print(f"\n  오늘 선별 {top_n}개: {', '.join(selected)}")
        if not args.dry_run:
            update_symbols(selected, dry_run=False)
        else:
            update_symbols(selected, dry_run=True)

    print()


if __name__ == "__main__":
    main()
