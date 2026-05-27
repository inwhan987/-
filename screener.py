"""종목 스크리너 — 코스피 상위 N개 재무+기술 종합 분석 → SYMBOLS 자동 선별.

[매주 월요일 8:00]
  코스피 상위 market_top 개 → 재무+기술 종합 분석 → 상위 top_n 개 → SYMBOLS 업데이트

사용:
  python screener.py --mode weekly            # 분석 + SYMBOLS 업데이트
  python screener.py --mode weekly --dry-run  # 업데이트 없이 점수만 출력
  python screener.py --mode weekly --top 6 --market-top 200
"""
from __future__ import annotations
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="pykrx")
import io, os, sys, re, time, tempfile, shutil, argparse
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# .env / .env.overrides 에서 환경 변수 로드 (DART_API_KEY 등 시크릿)
def _load_dotenv() -> None:
    _here = Path(__file__).parent
    for fname in (".env", ".env.overrides"):
        p = _here / fname
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip(); v = v.split("#")[0].strip()
                if k and k not in os.environ:
                    os.environ[k] = v
_load_dotenv()

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)

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
import contextlib
import io
import requests
import pandas as pd
import numpy as np
import yfinance as yf

# yfinance 전체 로거 계층 억제
for _yf_log in ("yfinance", "yfinance.base", "yfinance.utils",
                "yfinance.scrapers", "peewee"):
    logging.getLogger(_yf_log).setLevel(logging.CRITICAL)


# ── Yahoo Finance 인증 세션 (yfinance 1.3+ requires curl_cffi) ──────
import threading as _yf_threading
_YF_SESSION = None
_YF_SESSION_LOCK = _yf_threading.Lock()

def _get_yf_session():
    """curl_cffi 브라우저 임퍼소네이션 세션 (싱글턴, 스레드 안전)."""
    global _YF_SESSION
    if _YF_SESSION is not None:
        return _YF_SESSION
    with _YF_SESSION_LOCK:
        if _YF_SESSION is not None:  # double-check
            return _YF_SESSION
        try:
            from curl_cffi import requests as _cr
            # timeout=30: 단일 호출이 무한 hang 되어 워커 차단 + proc.wait timeout 트리거 방지
            _YF_SESSION = _cr.Session(impersonate="chrome", timeout=30)
        except ImportError:
            _YF_SESSION = requests.Session()
    return _YF_SESSION


@contextlib.contextmanager
def _quiet_yf():
    """yfinance 호출 — 과거에는 sys.stderr 를 글로벌로 교체했으나, 멀티스레드에서
    워커 간 race condition (다른 스레드의 stderr까지 변경/복원 꼬임) 으로
    서브프로세스 침묵 종료 원인이 되어 제거. yfinance 로거는 CRITICAL 로 이미
    억제되어 있고, _yf_download/_yf_ticker_info 가 try/except 로 감싸므로 안전."""
    yield


def _yf_ticker(sym: str) -> yf.Ticker:
    return yf.Ticker(sym, session=_get_yf_session())


def _yf_download(sym: str, **kwargs) -> pd.DataFrame:
    """yf.download 래퍼 — 인증 세션 사용, 실패 시 빈 DataFrame.

    auto_adjust=True 로 시도 후 close 컬럼이 전부 NaN 이면
    auto_adjust=False 로 재시도 (한국 주식 조정팩터 계산 실패 대응).
    """
    def _normalize(df: pd.DataFrame) -> pd.DataFrame:
        """MultiIndex 컬럼 → flat lowercase."""
        if df.empty:
            return df
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                      for c in df.columns]
        return df

    try:
        with _quiet_yf():
            df = yf.download(sym, session=_get_yf_session(), **kwargs)
        df = _normalize(df)
        # close 가 전부 NaN 이면 auto_adjust 실패 → False 로 재시도
        if not df.empty and "close" in df.columns and df["close"].isna().all():
            kwargs2 = {**kwargs, "auto_adjust": False}
            with _quiet_yf():
                df2 = yf.download(sym, session=_get_yf_session(), **kwargs2)
            df2 = _normalize(df2)
            # auto_adjust=False 시 'adj close' 를 'close' 로 사용
            if not df2.empty and "adj close" in df2.columns:
                df2["close"] = df2["adj close"]
            if not df2.empty and "close" in df2.columns and not df2["close"].isna().all():
                return df2
        return df
    except Exception as e:
        msg = str(e)
        if "401" in msg or "429" in msg or "Unauthorized" in msg:
            return pd.DataFrame()
        raise


def _yf_ticker_info(sym: str) -> dict:
    """Ticker.info — 인증 세션, 실패 시 빈 dict."""
    try:
        with _quiet_yf():
            return _yf_ticker(sym).info or {}
    except Exception:
        return {}


HERE = Path(__file__).parent
CANDIDATES_FILE  = HERE / "screener_candidates.txt"

# ── KOSPI 지수 수익률 캐시 (RS 계산용, 1회만 다운로드) ──────────────────────
_KOSPI_RET_CACHE: dict[str, float] = {}   # "20d" → float


def _get_kospi_return(days: int = 20) -> float:
    """KOSPI(^KS11) N일 수익률. 실패 시 0.0 반환."""
    key = f"{days}d"
    if key in _KOSPI_RET_CACHE:
        return _KOSPI_RET_CACHE[key]
    try:
        df = _yf_download("^KS11", period="120d", interval="1d",
                          auto_adjust=True, progress=False)
        if df.empty or len(df) < days + 1:
            _KOSPI_RET_CACHE[key] = 0.0
            return 0.0
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                      for c in df.columns]
        df = df[df["close"].notna()]
        if df.empty or len(df) < days + 1:
            _KOSPI_RET_CACHE[key] = 0.0
            return 0.0
        ret = float((df["close"].iloc[-1] / df["close"].iloc[-days - 1] - 1) * 100)
        _KOSPI_RET_CACHE[key] = ret
        return ret
    except Exception:
        _KOSPI_RET_CACHE[key] = 0.0
        return 0.0


def _calc_adx(df: pd.DataFrame, period: int = 14) -> tuple[float, float, float]:
    """ADX, +DI, -DI 반환. 데이터 부족 시 (0, 0, 0)."""
    try:
        hi, lo, cl = df["high"], df["low"], df["close"]
        tr = pd.concat([
            hi - lo,
            (hi - cl.shift(1)).abs(),
            (lo - cl.shift(1)).abs(),
        ], axis=1).max(axis=1)
        raw_p = (hi - hi.shift(1)).clip(lower=0)
        raw_m = (lo.shift(1) - lo).clip(lower=0)
        dm_p = raw_p.where(raw_p > raw_m, 0.0)
        dm_m = raw_m.where(raw_m > raw_p, 0.0)

        def _wilder(s: pd.Series, n: int) -> pd.Series:
            r = s.copy().astype(float) * float("nan")
            if len(s) < n + 1:
                return r
            r.iloc[n] = s.iloc[1:n + 1].sum()
            for i in range(n + 1, len(s)):
                r.iloc[i] = r.iloc[i - 1] - r.iloc[i - 1] / n + s.iloc[i]
            return r

        atr_s = _wilder(tr, period)
        dip_s = _wilder(dm_p, period)
        dim_s = _wilder(dm_m, period)
        di_p  = (dip_s / atr_s * 100).replace([np.inf, -np.inf], np.nan)
        di_m  = (dim_s / atr_s * 100).replace([np.inf, -np.inf], np.nan)
        dx    = ((di_p - di_m).abs() / (di_p + di_m) * 100).replace(
                    [np.inf, -np.inf], np.nan)
        adx_s = _wilder(dx.fillna(0), period)
        # _wilder initializes with sum (not mean) → divides by period to get true ADX
        return (float(adx_s.iloc[-1]) / period, float(di_p.iloc[-1]),
                float(di_m.iloc[-1]))
    except Exception:
        return 0.0, 0.0, 0.0

# ══════════════════════════════════════════════════════════════════
#  DART(전자공시) 재무 데이터 헬퍼
# ══════════════════════════════════════════════════════════════════
import threading as _threading
_DART_CLIENT   = None
_DART_CORP_DF  = None   # corp_codes DataFrame 캐시 (최초 1회 다운로드)
_DART_FIN_CACHE: dict[str, dict] = {}  # stock_code → 재무지표 캐시
_DART_CORP_LOCK = _threading.Lock()  # corp_codes ZIP 동시 다운로드 방지
_DART_API_LOCK  = _threading.Lock()  # finstate 직렬화 (속도제한 + stdout 억제)


def _get_dart():
    """OpenDartReader 클라이언트 (싱글턴)."""
    global _DART_CLIENT
    if _DART_CLIENT is None:
        try:
            try:
                from opendartreader import OpenDartReader as _odr
            except ImportError:
                import OpenDartReader as _odr
            api_key = os.environ.get("DART_API_KEY", "")
            if not api_key:
                raise RuntimeError("DART_API_KEY 환경 변수 없음")
            _DART_CLIENT = _odr(api_key)
        except ImportError:
            raise RuntimeError("opendartreader 미설치")
    return _DART_CLIENT


def _dart_corp_code(stock_code: str) -> str:
    """6자리 주식코드 → DART 8자리 corp_code. 실패 시 빈 문자열."""
    global _DART_CORP_DF
    try:
        dart = _get_dart()
        if _DART_CORP_DF is None:
            with _DART_CORP_LOCK:  # 동시 다운로드 방지 (race condition)
                if _DART_CORP_DF is None:
                    _DART_CORP_DF = dart.corp_codes  # ZIP 다운로드 (최초 1회)
        rows = _DART_CORP_DF[_DART_CORP_DF["stock_code"] == stock_code]
        return rows["corp_code"].values[0] if not rows.empty else ""
    except Exception:
        return ""


def _dart_finstate(dart, corp_code: str, year: int, rtype: str, retries: int = 2):
    """dart.finstate() 래퍼 — 락 직렬화 + 재시도 + 속도제한 방지.

    에러 dict(status!=000) 반환 시 최대 retries 회 재시도 (2s, 4s backoff).
    """
    _RTYPE_LABEL = {"11011": "연간", "11013": "Q1", "11012": "H1", "11014": "Q3"}
    label = _RTYPE_LABEL.get(rtype, rtype)
    for attempt in range(retries + 1):
        try:
            with _DART_API_LOCK:
                result = dart.finstate(corp_code, year, rtype)
                time.sleep(0.5)
        except Exception as e:
            print(f"  [DART ERR] corp={corp_code} {year}년 {label} → 예외: {e} (시도 {attempt+1}/{retries+1})", flush=True)
            if attempt < retries:
                time.sleep(2.0 * (attempt + 1))
            continue
        if not isinstance(result, dict):
            return result
        status  = result.get("status", "?")
        message = result.get("message", "?")
        print(f"  [DART {status}] corp={corp_code} {year}년 {label} → {message} (시도 {attempt+1}/{retries+1})", flush=True)
        if attempt < retries:
            time.sleep(2.0 * (attempt + 1))
    return None


def _dart_financials(stock_code: str) -> dict:
    """DART 연간+분기 재무제표에서 핵심 지표 추출.

    반환 키: revenueGrowth, earningsGrowth, returnOnEquity, debtToEquity,
             qtr_rev_growth, qtr_inc_growth, qtr_label
    DART API 속도제한 방지: 세마포어로 동시 호출 1개 제한.
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
        cur_month = datetime.now().month

        # ── 연간 보고서 ────────────────────────────────────────────────
        fs = None
        for y in [cur_year - 1, cur_year - 2]:
            try:
                tmp = _dart_finstate(dart, corp_code, y, "11011")
                if tmp is not None and not (isinstance(tmp, pd.DataFrame) and tmp.empty):
                    tmp = tmp if isinstance(tmp, pd.DataFrame) else pd.DataFrame(tmp)
                    if not tmp.empty:
                        for div in ["CFS", "OFS"]:
                            sub = tmp[tmp["fs_div"] == div] if "fs_div" in tmp.columns else tmp
                            if not sub.empty:
                                fs = sub
                                break
            except Exception:
                pass
            if fs is not None and not fs.empty:
                break

        def _get(df, *keywords, col: str = "thstrm_amount") -> float | None:
            """account_nm에서 keyword(들) 순서대로 검색, 첫 번째 매칭값 반환."""
            if df is None or df.empty:
                return None
            for kw in keywords:
                rows = df[df["account_nm"].str.contains(kw, na=False, regex=False)]
                if rows.empty:
                    continue
                raw = rows.iloc[0][col]
                try:
                    v = float(str(raw).replace(",", "").replace(" ", ""))
                    if v != 0:
                        return v
                except Exception:
                    pass
            return None

        if fs is not None and not fs.empty:
            # 계정명은 회사/보고서마다 다를 수 있어 우선순위 순으로 fallback 시도
            rev_cur = _get(fs, "매출액", "영업수익", "수익(매출액)")
            rev_prv = _get(fs, "매출액", "영업수익", "수익(매출액)", col="frmtrm_amount")
            inc_cur = _get(fs, "당기순이익", "당기순이익(손실)")
            inc_prv = _get(fs, "당기순이익", "당기순이익(손실)", col="frmtrm_amount")
            equity  = _get(fs, "자본총계")
            debt    = _get(fs, "부채총계")

            if rev_cur and rev_prv:
                result["revenueGrowth"]  = (rev_cur - rev_prv) / abs(rev_prv)
            if inc_cur and inc_prv:
                result["earningsGrowth"] = (inc_cur - inc_prv) / abs(inc_prv)
            if inc_cur and equity:
                result["returnOnEquity"] = inc_cur / abs(equity)
            if debt is not None and equity:
                result["debtToEquity"]   = (debt / abs(equity)) * 100

        # ── 분기 보고서 (최근 분기 YoY) ────────────────────────────────
        # 현재 월 기준으로 제출된 가장 최근 분기 추정:
        #   ~4월: Q3(전년) / 5~7월: Q1(당년) / 8~10월: H1(당년) / 11~: Q3(당년)
        _QTR_CANDIDATES = []
        if cur_month >= 11:
            _QTR_CANDIDATES = [(cur_year, "11014"), (cur_year, "11012"), (cur_year - 1, "11014")]
        elif cur_month >= 8:
            _QTR_CANDIDATES = [(cur_year, "11012"), (cur_year, "11014"), (cur_year - 1, "11014")]
        elif cur_month >= 5:
            _QTR_CANDIDATES = [(cur_year, "11013"), (cur_year - 1, "11014"), (cur_year - 1, "11012")]
        else:
            _QTR_CANDIDATES = [(cur_year - 1, "11014"), (cur_year - 1, "11012"), (cur_year - 1, "11013")]

        _QTR_LABELS = {"11013": "Q1", "11012": "H1", "11014": "Q3"}

        qfs_cur = None
        qfs_prv = None
        qtr_label = ""
        for (qy, qtype) in _QTR_CANDIDATES:
            try:
                tmp = _dart_finstate(dart, corp_code, qy, qtype)
                if tmp is not None and not (isinstance(tmp, pd.DataFrame) and tmp.empty):
                    tmp = tmp if isinstance(tmp, pd.DataFrame) else pd.DataFrame(tmp)
                    if not tmp.empty:
                        for div in ["CFS", "OFS"]:
                            sub = tmp[tmp["fs_div"] == div] if "fs_div" in tmp.columns else tmp
                            if not sub.empty:
                                qfs_cur = sub
                                qtr_label = f"{qy} {_QTR_LABELS.get(qtype, qtype)}"
                                # 전년 동분기
                                try:
                                    tmp2 = _dart_finstate(dart, corp_code, qy - 1, qtype)
                                    if tmp2 is not None and not (isinstance(tmp2, pd.DataFrame) and tmp2.empty):
                                        tmp2 = tmp2 if isinstance(tmp2, pd.DataFrame) else pd.DataFrame(tmp2)
                                        if not tmp2.empty:
                                            for div2 in ["CFS", "OFS"]:
                                                sub2 = tmp2[tmp2["fs_div"] == div2] if "fs_div" in tmp2.columns else tmp2
                                                if not sub2.empty:
                                                    qfs_prv = sub2
                                                    break
                                except Exception:
                                    pass
                                break
            except Exception:
                pass
            if qfs_cur is not None:
                break

        if qfs_cur is not None and qfs_prv is not None:
            qrev_cur = _get(qfs_cur, "매출액", "영업수익", "수익(매출액)")
            qrev_prv = _get(qfs_prv, "매출액", "영업수익", "수익(매출액)")
            qinc_cur = _get(qfs_cur, "당기순이익", "당기순이익(손실)")
            qinc_prv = _get(qfs_prv, "당기순이익", "당기순이익(손실)")
            if qrev_cur and qrev_prv and abs(qrev_prv) > 0:
                result["qtr_rev_growth"] = (qrev_cur - qrev_prv) / abs(qrev_prv)
            if qinc_cur and qinc_prv and abs(qinc_prv) > 0:
                result["qtr_inc_growth"] = (qinc_cur - qinc_prv) / abs(qinc_prv)
            if qtr_label:
                result["qtr_label"] = qtr_label

    except Exception:
        pass

    # 데이터가 있을 때만 캐시 (빈 결과는 캐시 안 함 → 다음 실행 시 재시도 가능)
    if result:
        _DART_FIN_CACHE[stock_code] = result
    return result


# ══════════════════════════════════════════════════════════════════
#  wisereport(Naver 금융 백엔드) — 연간 EPS 실적/컨센서스 + PER
# ══════════════════════════════════════════════════════════════════
_WISEREPORT_CACHE: dict[str, dict] = {}


def _wisereport_eps(stock_code: str) -> dict:
    """wisereport cF1002.aspx에서 연간 EPS 실적(A)/추정(E) + PER 추출.

    반환 키:
      eps_actuals  : [(year, eps), ...]  오래된 순, 실적
      eps_forward  : (year, eps) | None  가장 가까운 추정치
      per_actual   : float | None        최근 실적 PER
      per_forward  : float | None        포워드 PER
    """
    if stock_code in _WISEREPORT_CACHE:
        return _WISEREPORT_CACHE[stock_code]

    result: dict = {
        "eps_actuals": [], "eps_forward": None,
        "per_actual": None, "per_forward": None,
    }
    try:
        url = "https://navercomp.wisereport.co.kr/company/cF1002.aspx"
        hdrs = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": (
                f"https://navercomp.wisereport.co.kr/v2/company/c1050001.aspx"
                f"?cmp_cd={stock_code}"
            ),
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = requests.get(
            url, params={"cmp_cd": stock_code, "finGubun": "0"},
            headers=hdrs, timeout=15,
        )
        if resp.status_code != 200 or len(resp.text) < 200:
            _WISEREPORT_CACHE[stock_code] = result
            return result

        tables = pd.read_html(io.StringIO(resp.text))
        for t in tables:
            # 멀티인덱스 컬럼 평탄화
            if isinstance(t.columns, pd.MultiIndex):
                t.columns = [
                    " ".join(str(c) for c in col if str(c) != "nan").strip()
                    for col in t.columns
                ]
            cols_str = " ".join(str(c) for c in t.columns)
            if "EPS" not in cols_str:
                continue

            eps_col = next((c for c in t.columns if "EPS" in str(c)), None)
            per_col = next(
                (c for c in t.columns
                 if "PER" in str(c) and "EPS" not in str(c) and "EBITDA" not in str(c)),
                None,
            )
            if eps_col is None:
                continue

            year_col = t.columns[0]
            for _, row in t.iterrows():
                label = str(row[year_col]).strip()
                m = re.match(r"(\d{4})\(([AE])\)", label)
                if not m:
                    continue
                year, kind = int(m.group(1)), m.group(2)

                def _v(col):
                    if col is None:
                        return None
                    try:
                        return float(str(row[col]).replace(",", "").strip())
                    except Exception:
                        return None

                eps = _v(eps_col)
                per = _v(per_col)
                if eps is None or eps == 0:
                    continue

                if kind == "A":
                    result["eps_actuals"].append((year, eps))
                    if per and result["per_actual"] is None:
                        result["per_actual"] = per
                elif kind == "E" and result["eps_forward"] is None:
                    result["eps_forward"] = (year, eps)
                    if per:
                        result["per_forward"] = per

            result["eps_actuals"].sort(key=lambda x: x[0])
            break

    except Exception:
        pass

    _WISEREPORT_CACHE[stock_code] = result
    return result


# ── 네이버 금융 업종명 캐시 ────────────────────────────────────────────────
_NAVER_SECTOR_CACHE: dict[str, str] = {}   # stock_code → 업종명
_NAVER_GROUP_CACHE:  dict[str, str] = {}   # upjong_no  → 업종명

_NAVER_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _naver_group_name(upjong_no: str) -> str:
    """upjong 그룹 번호 → 업종명 (페이지 title 파싱). 캐시."""
    if upjong_no in _NAVER_GROUP_CACHE:
        return _NAVER_GROUP_CACHE[upjong_no]
    result = ""
    try:
        url = (
            f"https://finance.naver.com/sise/sise_group_detail.naver"
            f"?type=upjong&no={upjong_no}"
        )
        resp = requests.get(url, headers=_NAVER_HDR, timeout=10)
        resp.encoding = "euc-kr"
        m = re.search(r"<title>\s*([^:<\n]+?)\s*(?::\s*Npay|</title>)", resp.text)
        if m:
            result = m.group(1).strip()
    except Exception:
        pass
    _NAVER_GROUP_CACHE[upjong_no] = result
    return result


def _naver_industry(stock_code: str) -> str:
    """네이버 금융 coinfo 페이지 → upjong 번호 → 업종명. 실패 시 ''."""
    if stock_code in _NAVER_SECTOR_CACHE:
        return _NAVER_SECTOR_CACHE[stock_code]
    result = ""
    try:
        url = f"https://finance.naver.com/item/coinfo.naver?code={stock_code}"
        resp = requests.get(url, headers=_NAVER_HDR, timeout=10)
        resp.encoding = "euc-kr"
        m = re.search(r"upjong&no=(\d+)", resp.text)
        if m:
            result = _naver_group_name(m.group(1))
    except Exception:
        pass
    _NAVER_SECTOR_CACHE[stock_code] = result
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
    "Banks - Diversified":                       "은행",
    "Banks - Regional":                          "은행",
    "Capital Markets":                           "증권",
    "Insurance—Life":                            "보험",
    "Insurance—Diversified":                     "보험",
    "Insurance—Property & Casualty":             "보험",
    "Insurance - Life":                          "보험",
    "Insurance - Diversified":                   "보험",
    "Insurance - Property & Casualty":           "보험",
    # 바이오/제약
    "Biotechnology":                             "바이오",
    "Drug Manufacturers—General":                "제약",
    "Drug Manufacturers—Specialty & Generic":    "제약",
    "Drug Manufacturers - General":              "제약",
    "Drug Manufacturers - Specialty & Generic":  "제약",
    # 소재/에너지
    "Steel":                                     "철강",
    "Aluminum":                                  "비철금속",
    "Oil & Gas Integrated":                      "정유",
    "Oil & Gas Refining & Marketing":            "정유",
    # 통신
    "Telecom Services":                          "통신",
    "Wireless Telecom Services":                 "통신",
    # 산업재
    "Engineering & Construction":                "건설",
    "Specialty Industrial Machinery":            "기계/중공업",
    "Industrial Machinery":                      "기계/중공업",
    "Aerospace & Defense":                       "방산/항공",
    "Diversified Industrials":                   "복합산업",
    "Marine Shipping":                           "조선/해운",
    "Shipping & Ports":                          "조선/해운",
    # 유통/소비재
    "Grocery Stores":                            "유통/마트",
    "Discount Stores":                           "유통/마트",
    "Department Stores":                         "유통/백화점",
    "Apparel Retail":                            "의류/패션",
    "Apparel Manufacturing":                     "의류/패션",
    "Textile Manufacturing":                     "섬유/의류",
    "Packaged Foods":                            "식품",
    "Food Distribution":                         "식품유통",
    "Beverages—Non-Alcoholic":                   "음료",
    "Beverages - Non-Alcoholic":                 "음료",
    "Restaurants":                               "외식",
    # 서비스/기타
    "Rental & Leasing Services":                 "렌탈",
    "Integrated Freight & Logistics":            "물류",
    "Air Freight & Logistics":                   "물류",
    "Entertainment":                             "엔터테인먼트",
    "Leisure":                                   "레저",
    "Hotels & Motel Chains":                     "호텔/숙박",
    "Conglomerates":                             "지주회사",
    "Tools & Accessories":                       "공구/부품",
    "Building Materials":                        "건자재",
    "Building Products & Equipment":             "건자재",
    "Paper & Paper Products":                    "제지",
    "Rubber & Plastics":                         "고무/플라스틱",
    # 부동산
    "REIT - Industrial":                         "리츠",
    "REIT - Retail":                             "리츠",
    "REIT - Office":                             "리츠",
    "Real Estate - General":                     "부동산",
    "Real Estate Services":                      "부동산서비스",
}
# 역방향 (한글 → 영문 키 목록)
_INDUSTRY_MAP_REV: dict[str, list[str]] = {}
for _eng, _kor in INDUSTRY_MAP.items():
    _INDUSTRY_MAP_REV.setdefault(_kor, []).append(_eng)
OVERRIDES_FILE   = HERE / ".env.overrides"

# ── 점수 가중치 ──────────────────────────────────────────────────────────────
# 총점 = (tech / TECH_MAX) * W_TECH * 10 + (fund / FUND_MAX) * W_FUND * 10
# tech: 항목별 이론 최대 합산 = 25.5  (SMA20+2, SMA60+2, RSI+2, ROC20+3, ROC60+3,
#       거래량+1.5, 52주+1.5, ST+1, 월봉EMA6+2, RS20+3, ADX+1.5, RS60+3)
# fund: 항목별 이론 최대 합산 = 27.5  (PER+3, ROE+3, 매출성장+3, 이익성장+3, 부채+1.5,
#       분기매출+3, 분기순이익+3, 서프라이즈+3, 연속비트+3, EPS추세+2)
# 최소 게이트: tech < TECH_MIN_GATE 면 재무 무관 자동 제외 (하락추세 종목 차단)
W_TECH        = 0.60   # 기술적 분석 비중
W_FUND        = 0.40   # 재무제표 비중
TECH_MAX      = 25.5   # tech_score 이론 최대값 (항목별 만점 합산 기준)
FUND_MAX      = 27.5   # fund_score 이론 최대값 (항목별 만점 합산 기준)
TECH_MIN_GATE = 0.0    # 이 값 미만이면 재무 무관 자동 제외 (하락추세 종목 차단)


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
            except BaseException:  # pykrx 내부 sys.exit 방어
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
        for ticker in fallback:
            _ = SYM_NAMES.get(ticker)
        return fallback

    # ── 시가총액 기준 정렬 (top_n 지정 시) ───────────────────────────
    if top_n > 0:
        cap_map: dict[str, int] = {}
        try:
            from datetime import timedelta as _td
            _try = datetime.strptime(date_str, "%Y%m%d")
            for _ in range(7):          # 최대 7일 전까지 재시도
                while _try.weekday() >= 5:   # 주말이면 하루 더 당김
                    _try -= _td(days=1)
                cap_df = _krx.get_market_cap(_try.strftime("%Y%m%d"))
                if cap_df is not None and not cap_df.empty:
                    cap_col = next(
                        (c for c in cap_df.columns
                         if "시가총액" in str(c) or "Mktcap" in str(c).lower()),
                        None,
                    )
                    if cap_col:
                        for code, row in cap_df.iterrows():
                            try:
                                iv = int(row[cap_col])
                                if iv > 0:
                                    cap_map[str(code)] = iv
                            except Exception:
                                pass
                if cap_map:             # 데이터 취득 성공
                    break
                _try -= _td(days=1)     # 실패 → 하루 전 재시도
        except BaseException:  # pykrx 내부 sys.exit 방어
            pass

        if cap_map:
            print(f"  [시총정렬] KRX API ({len(cap_map)}개 종목)")
            all_codes.sort(key=lambda x: cap_map.get(x[0], 0), reverse=True)
        else:
            print(f"  [시총정렬] 내장 폴백 (KRX API 데이터 없음)")
            fallback_rank: dict[str, int] = {}
            for i, sym in enumerate(_FALLBACK_KOSPI + _FALLBACK_KOSDAQ):
                fallback_rank[sym.split(".")[0]] = len(_FALLBACK_KOSPI) + len(_FALLBACK_KOSDAQ) - i
            all_codes.sort(key=lambda x: fallback_rank.get(x[0], 0), reverse=True)

        all_codes = all_codes[:top_n]

    result = []
    for code, suffix in all_codes:
        ticker = code + suffix
        result.append(ticker)
        if ticker not in SYM_NAMES:
            try:
                SYM_NAMES[ticker] = _krx.get_market_ticker_name(code)
            except BaseException:
                # SystemExit/KeyboardInterrupt 포함 — pykrx 내부에서 sys.exit() 호출
                # 시 web 컨텍스트에서 subprocess 가 exit 0 으로 조용히 종료되던 원인.
                # 이름 조회 실패해도 코드로 폴백.
                SYM_NAMES[ticker] = code

    return result




# ══════════════════════════════════════════════════════════════════
#  기술적 분석 점수 (0 ~ 10)
# ══════════════════════════════════════════════════════════════════
def tech_score(sym: str) -> tuple[float, dict]:
    """일봉 1년 데이터로 기술적 점수 계산. 음수 가능 (하락추세 종목 자동 필터)."""
    detail = {}
    try:
        df = _yf_download(sym, period="1y", interval="1d",
                          auto_adjust=True, progress=False)
        if df.empty or len(df) < 20:
            return 0.0, {"error": "no data"}
        df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                      for c in df.columns]
        # yfinance 가 당일 미확정 데이터를 NaN 으로 반환하는 경우 제거
        df = df[df["close"].notna()]
        if df.empty or len(df) < 20:
            return 0.0, {"error": "no valid close data"}
        close = df["close"]
        volume = df["volume"]

        score = 0.0

        # 1) 20일 SMA 위치 — 이격도 기반 (+2/+1.5/+1/-0.5/-1)
        sma20 = float(close.rolling(20).mean().iloc[-1])
        cur   = float(close.iloc[-1])
        dev20 = (cur - sma20) / sma20 * 100  # 이격도 %
        if dev20 > 10:
            score += 2      # 강한 상승모멘텀
        elif dev20 > 3:
            score += 1.5
        elif dev20 > 0:
            score += 1
        elif dev20 > -3:
            score -= 0.5
        else:
            score -= 1
        detail["SMA20"] = f"{'위' if dev20>0 else '아래'} ({cur:.0f} vs {sma20:.0f}, {dev20:+.1f}%)"

        # 2) 60일 SMA 위치 — 이격도 기반 (+2/+1.5/+1/-0.5/-1)
        if len(close) >= 60:
            sma60 = float(close.rolling(60).mean().iloc[-1])
            dev60 = (cur - sma60) / sma60 * 100
            if dev60 > 15:
                score += 2
            elif dev60 > 5:
                score += 1.5
            elif dev60 > 0:
                score += 1
            elif dev60 > -5:
                score -= 0.5
            else:
                score -= 1
            detail["SMA60"] = f"{'위' if dev60>0 else '아래'} ({sma60:.0f}, {dev60:+.1f}%)"
        else:
            detail["SMA60"] = "데이터부족"

        # 3) RSI (+2/+1.5/+1/-0.5) — 과매도 구간 패널티 추가
        #   45~72: 건강한 상승 (+2)
        #   72~82: 강한 추세 (+1.5)
        #   >82  : 매우 강한 모멘텀 (+1.0)
        #   35~45: 약세권 (-0.5)
        #   <35  : 과매도/하락추세 (-1)
        delta = close.diff()
        gain  = delta.clip(lower=0).rolling(14).mean()
        loss  = (-delta.clip(upper=0)).rolling(14).mean()
        rs    = gain / loss.replace(0, np.nan)
        rsi   = float((100 - 100 / (1 + rs)).iloc[-1])
        if 45 <= rsi <= 72:
            score += 2
        elif 72 < rsi <= 82:
            score += 1.5
        elif rsi > 82:
            score += 1.0
        elif rsi < 35:
            score -= 1
        elif rsi < 45:
            score -= 0.5
        detail["RSI14"] = f"{rsi:.1f}"

        # 4) 20일 수익률 — 단기 모멘텀 (+3/+2.5/+2/+1.5/+1/+0.5/-1/-2)
        ret20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100) if len(close) >= 21 else 0.0
        if ret20 > 50:
            score += 3      # 압도적 단기 모멘텀
        elif ret20 > 20:
            score += 2.5
        elif ret20 > 10:
            score += 2
        elif ret20 > 5:
            score += 1.5
        elif ret20 > 3:
            score += 1
        elif ret20 > 0:
            score += 0.5
        elif ret20 < -5:
            score -= 2
        elif ret20 < -2:
            score -= 1
        detail["ROC20"] = f"{ret20:+.1f}%"

        # 4b) 60일 수익률 — 중기 모멘텀 (+3/+2.5/+2/+1.5/+1/-1/-2)
        ret60 = float((close.iloc[-1] / close.iloc[-61] - 1) * 100) if len(close) >= 62 else 0.0
        if ret60 > 80:
            score += 3      # 압도적 중기 모멘텀
        elif ret60 > 50:
            score += 2.5
        elif ret60 > 30:
            score += 2
        elif ret60 > 15:
            score += 1.5
        elif ret60 > 10:
            score += 1
        elif ret60 < -10:
            score -= 2
        elif ret60 < 0:
            score -= 1
        detail["ROC60"] = f"{ret60:+.1f}%"

        # 5) 거래량 (+1.5/+1/-0.5) — 매우 높은 거래량 추가, 저조 패널티
        vol5  = float(volume.iloc[-5:].mean())
        vol20 = float(volume.iloc[-20:].mean())
        vol_ratio = vol5 / vol20 if vol20 > 0 else 1.0
        if vol_ratio > 2.0:
            score += 1.5    # 폭발적 거래량
        elif vol_ratio > 1.1:
            score += 1
        elif vol_ratio < 0.5:
            score -= 0.5    # 거래량 급감 (관심 이탈 신호)
        detail["거래량"] = f"{'급증' if vol_ratio>2 else '증가' if vol_ratio>1.1 else '저조' if vol_ratio<0.5 else '보통'} (5일평균 {vol_ratio:.2f}x)"

        # 6) 52주 고점 대비 위치 (+1.5/+1/0/-1/-2/-3)
        #   ≥95%: +1.5 (신고가권) / ≥80%: +1 / 65~80%: 0
        #   50~65%: -1 / 30~50%: -2 / <30%: -3 (급락종목)
        high52 = float(close.rolling(min(len(close), 252)).max().iloc[-1])
        pos52  = cur / high52 * 100
        if pos52 >= 95:
            score += 1.5    # 신고가권
        elif pos52 >= 80:
            score += 1
        elif pos52 < 30:
            score -= 3      # 급락종목
        elif pos52 < 50:
            score -= 2
        elif pos52 < 65:
            score -= 1
        detail["52주고점"] = f"{pos52:.0f}%"

        # 7) Supertrend 방향 (+1/-1) — 봇 핵심 지표, 하락추세 패널티 추가
        try:
            high_s = df["high"]
            low_s  = df["low"]
            hl  = high_s - low_s
            hpc = (high_s - close.shift(1)).abs()
            lpc = (low_s  - close.shift(1)).abs()
            tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
            atr = tr.rolling(7).mean()
            mult = 3.0
            ub = ((high_s + low_s) / 2 + mult * atr).ffill()
            lb = ((high_s + low_s) / 2 - mult * atr).ffill()
            st_dir = 1
            for i in range(-10, 0):
                c = float(close.iloc[i])
                if st_dir == 1:
                    st_dir = -1 if c < float(lb.iloc[i]) else 1
                else:
                    st_dir = 1  if c > float(ub.iloc[i]) else -1
            if st_dir == 1:
                score += 1
            else:
                score -= 1   # 하락추세 패널티
            detail["Supertrend"] = "상승" if st_dir == 1 else "하락"
        except Exception:
            detail["Supertrend"] = "N/A"

        # 8) 월봉 EMA(6) 위치 — 이격도 기반 (+2/+1.5/-1/-2)
        try:
            monthly_close = close.resample("ME").last().dropna()
            if len(monthly_close) >= 6:
                ema6_m = float(monthly_close.ewm(span=6, adjust=False).mean().iloc[-1])
                dev_ema6m = (cur - ema6_m) / ema6_m * 100
                if dev_ema6m > 10:
                    score += 2
                elif dev_ema6m > 0:
                    score += 1.5
                elif dev_ema6m > -5:
                    score -= 1
                else:
                    score -= 2
                detail["월봉EMA6"] = f"{'위' if dev_ema6m>0 else '아래'} ({ema6_m:.0f}, {dev_ema6m:+.1f}%)"
            else:
                detail["월봉EMA6"] = "데이터부족"
        except Exception:
            detail["월봉EMA6"] = "N/A"

        # 9) RS vs KOSPI 20일 (+3/+2.5/+2/+1.5/+1/-0.5/-1/-2) — 단기 상대강도
        try:
            kospi_ret20 = _get_kospi_return(20)
            rs_val = ret20 - kospi_ret20
            if rs_val > 30:
                score += 3      # 압도적 초과수익
            elif rs_val > 15:
                score += 2.5
            elif rs_val > 5:
                score += 2
            elif rs_val > 2:
                score += 1.5
            elif rs_val > 0:
                score += 1
            elif rs_val > -5:
                score -= 0.5
            elif rs_val > -10:
                score -= 1
            else:
                score -= 2
            detail["RS_KOSPI"] = f"{rs_val:+.1f}%p (주식{ret20:+.1f} 코스피{kospi_ret20:+.1f})"
        except Exception:
            detail["RS_KOSPI"] = "N/A"

        # 10) ADX 방향 (+1.5/+1/-1/-2/-3) — 추세 강도 및 방향
        try:
            adx_val, di_p, di_m = _calc_adx(df)
            if adx_val > 35 and di_p > di_m:
                score += 1.5    # 매우 강한 상승추세
            elif adx_val > 25 and di_p > di_m:
                score += 1      # 강한 상승추세
            elif adx_val < 15:
                score -= 2      # 추세 없음 (강한 횡보)
            elif adx_val < 20:
                score -= 1      # 약한 추세
            elif adx_val > 25 and di_m > di_p:
                score -= 3      # 강한 하락추세
            detail["ADX"] = f"{adx_val:.1f} (+DI{di_p:.1f} -DI{di_m:.1f})"
        except Exception:
            detail["ADX"] = "N/A"

        # 11) RS vs KOSPI 60일 (+3/+2.5/+2/+1.5/+1/-0.5/-1/-2) — 중기 상대강도
        try:
            kospi_ret60 = _get_kospi_return(60)
            rs60_val = ret60 - kospi_ret60
            if rs60_val > 50:
                score += 3      # 압도적 중기 초과수익
            elif rs60_val > 25:
                score += 2.5
            elif rs60_val > 10:
                score += 2
            elif rs60_val > 5:
                score += 1.5
            elif rs60_val > 0:
                score += 1
            elif rs60_val > -8:
                score -= 0.5
            elif rs60_val > -15:
                score -= 1
            else:
                score -= 2
            detail["RS60_KOSPI"] = f"{rs60_val:+.1f}%p (주식{ret60:+.1f} 코스피{kospi_ret60:+.1f})"
        except Exception:
            detail["RS60_KOSPI"] = "N/A"

        return max(min(score, 25.5), -14.0), detail

    except Exception as e:
        return 0.0, {"error": str(e)[:60]}


# ══════════════════════════════════════════════════════════════════
#  재무제표 점수 (0 ~ 15)
# ══════════════════════════════════════════════════════════════════
def fundamental_score(sym: str) -> tuple[float, dict]:
    """DART + wisereport(Naver 백엔드) 기반 재무 점수 계산. yfinance 미사용.

    데이터 소스:
      DART      : ROE, 매출성장, 이익성장, 부채비율 (4개)
      wisereport: PER(forward), EPS 실적/추정 시계열 (4개)

    배점:
      1) PER          (+3)  wisereport forward PER
      2) ROE          (+2)  DART
      3) 매출성장      (+2)  DART
      4) 이익성장      (+2)  DART
      5) 부채비율      (+1)  DART
      6) 최근 EPS 성장 (+2)  wisereport YoY 성장률
      7) 연속 EPS 성장 (+2)  wisereport 연속 성장 연수
      8) 포워드 컨센서스(+1) wisereport 추정치 성장률
    """
    detail: dict = {}
    score  = 0.0
    stock_code = sym.split(".")[0]  # "005930.KS" → "005930"

    # ── wisereport: EPS 시계열 + PER ─────────────────────────────────
    wr: dict = {}
    try:
        wr = _wisereport_eps(stock_code)
    except Exception:
        pass

    eps_actuals: list[tuple[int, float]] = wr.get("eps_actuals", [])  # 오래된 순
    eps_forward: tuple[int, float] | None = wr.get("eps_forward")
    per_forward = wr.get("per_forward")
    per_actual  = wr.get("per_actual")

    # 섹터 정보: 네이버 금융 우선, yfinance 폴백
    detail["sector"] = ""
    detail["industry"] = ""
    naver_ind = _naver_industry(stock_code)
    if naver_ind:
        detail["industry"] = naver_ind
        detail["sector"]   = naver_ind
    try:
        info = _yf_ticker_info(sym)
        raw_s = info.get("sector",   "")
        raw_i = info.get("industry", "")
        detail["sector_en"]   = raw_s
        detail["industry_en"] = raw_i
        if not naver_ind:
            # 네이버 데이터 없을 때만 yfinance 사용
            kor_industry = INDUSTRY_MAP.get(raw_i, "")
            detail["industry"] = kor_industry or SECTOR_MAP.get(raw_s, "")
            detail["sector"]   = SECTOR_MAP.get(raw_s, raw_s)
    except Exception:
        pass

    # 1) PER (+3~+0.5) — actual/forward 중 낮은 쪽(저평가 우선) 사용
    # 낮은 PER = 더 저렴 → 유리한 쪽 채택. 둘 다 없으면 N/A.
    _per_candidates = [v for v in [per_actual, per_forward] if v is not None and v > 0]
    fpe = min(_per_candidates) if _per_candidates else None
    _per_src = ""
    if fpe is not None:
        if per_actual and per_forward:
            _per_src = "(act)" if fpe == per_actual else "(fwd)"
        elif per_forward:
            _per_src = "(fwd)"
        if fpe < 8:
            score += 3
        elif fpe < 15:
            score += 2.5
        elif fpe < 20:
            score += 2
        elif fpe < 30:
            score += 1.5
        elif fpe < 40:
            score += 1
        elif fpe < 60:
            score += 0.5
        detail["PER"] = f"{fpe:.1f}{_per_src}"
    else:
        detail["PER"] = "N/A"

    # ── DART: 핵심 재무지표 ───────────────────────────────────────────
    dart: dict = {}
    try:
        dart = _dart_financials(stock_code)
        if not dart:
            detail["DART"] = f"데이터없음(corp={_dart_corp_code(stock_code) or '미발견'})"
    except Exception as _de:
        detail["DART오류"] = str(_de)[:80]

    # 2) ROE (+3/+2.5/+2/+1.5/+1)
    roe = dart.get("returnOnEquity")
    if roe is not None:
        if roe > 0.30:
            score += 3      # 압도적 자본효율 (30%+)
        elif roe > 0.20:
            score += 2.5
        elif roe > 0.15:
            score += 2
        elif roe > 0.10:
            score += 1.5
        elif roe > 0.05:
            score += 1
        detail["ROE"] = f"{roe*100:.1f}%"
    else:
        detail["ROE"] = "N/A"

    # 3) 매출성장 (+3/+2.5/+2/+1)
    rev_g = dart.get("revenueGrowth")
    if rev_g is not None:
        if rev_g > 0.30:
            score += 3      # 압도적 매출성장 (30%+)
        elif rev_g > 0.15:
            score += 2.5
        elif rev_g > 0.05:
            score += 2
        elif rev_g > 0:
            score += 1
        detail["매출성장"] = f"{rev_g*100:+.1f}%"
    else:
        detail["매출성장"] = "N/A"

    # 4) 이익성장 (+3/+2.5/+2/+1.5/+1)
    earn_g = dart.get("earningsGrowth")
    if earn_g is not None:
        if earn_g > 1.00:
            score += 3      # 압도적 이익성장 (100%+)
        elif earn_g > 0.50:
            score += 2.5
        elif earn_g > 0.10:
            score += 2
        elif earn_g > 0.05:
            score += 1.5
        elif earn_g > 0:
            score += 1
        detail["이익성장"] = f"{earn_g*100:+.1f}%"
    else:
        detail["이익성장"] = "N/A"

    # 5) 부채비율 (+1.5/+1/+0.5/-0.5)
    dte = dart.get("debtToEquity")
    if dte is not None:
        if dte < 30:
            score += 1.5    # 매우 건전한 재무구조
        elif dte < 50:
            score += 1
        elif dte < 100:
            score += 0.5
        elif dte > 200:
            score -= 0.5    # 과도한 부채 패널티
        detail["부채비율"] = f"{dte:.0f}%"
    else:
        detail["부채비율"] = "N/A"

    # 9) 분기 매출 YoY (+3/+2.5/+2/+1.5/+1/-0.5/-1)
    qtr_rev_g = dart.get("qtr_rev_growth")
    qtr_label  = dart.get("qtr_label", "최근분기")
    if qtr_rev_g is not None:
        if qtr_rev_g > 1.00:
            score += 3      # 압도적 분기 매출성장 (100%+)
        elif qtr_rev_g > 0.50:
            score += 2.5
        elif qtr_rev_g > 0.10:
            score += 2
        elif qtr_rev_g > 0.05:
            score += 1.5
        elif qtr_rev_g > 0:
            score += 1
        elif qtr_rev_g < -0.20:
            score -= 1
        elif qtr_rev_g < -0.05:
            score -= 0.5
        detail["분기매출YoY"] = f"{qtr_label} {qtr_rev_g*100:+.1f}%"
    else:
        detail["분기매출YoY"] = "N/A"

    # 10) 분기 순이익 YoY (+3/+2.5/+2/+1.5/+1/-0.5/-1)
    qtr_inc_g = dart.get("qtr_inc_growth")
    if qtr_inc_g is not None:
        if qtr_inc_g > 2.00:
            score += 3      # 압도적 분기 이익성장 (200%+)
        elif qtr_inc_g > 1.00:
            score += 2.5
        elif qtr_inc_g > 0.20:
            score += 2
        elif qtr_inc_g > 0.10:
            score += 1.5
        elif qtr_inc_g > 0:
            score += 1
        elif qtr_inc_g < -0.40:
            score -= 1
        elif qtr_inc_g < -0.10:
            score -= 0.5
        detail["분기순이익YoY"] = f"{qtr_label} {qtr_inc_g*100:+.1f}%"
    else:
        detail["분기순이익YoY"] = "N/A"

    # ── wisereport EPS 시계열 기반 서프라이즈 대체 점수 ──────────────
    try:
        eps_vals = [e for _, e in eps_actuals]  # float 리스트, 오래된 순

        if len(eps_vals) >= 2:
            latest   = eps_vals[-1]
            prev     = eps_vals[-2]

            # 6) 최근 EPS YoY 성장률 (+3/+2/+1.5/+1/+0.5/-0.5)
            if prev != 0:
                yoy = (latest - prev) / abs(prev)
                if yoy > 1.00:
                    score += 3
                    detail["최근서프라이즈"] = f"EPS YoY +{yoy*100:.0f}% (폭발)"
                elif yoy > 0.30:
                    score += 2
                    detail["최근서프라이즈"] = f"EPS YoY +{yoy*100:.0f}% (강)"
                elif yoy > 0.10:
                    score += 1.5
                    detail["최근서프라이즈"] = f"EPS YoY +{yoy*100:.0f}%"
                elif yoy > 0.05:
                    score += 1
                    detail["최근서프라이즈"] = f"EPS YoY +{yoy*100:.0f}% (소)"
                elif yoy > 0:
                    score += 0.5
                    detail["최근서프라이즈"] = f"EPS YoY +{yoy*100:.0f}% (미미)"
                else:
                    score -= 0.5
                    detail["최근서프라이즈"] = f"EPS YoY {yoy*100:.0f}% (감소)"
            else:
                detail["최근서프라이즈"] = "N/A"

            # 7) 연속 EPS 성장 연수 (+3/+2.5/+2/+1.5/+0.5)
            consec = 0
            for i in range(len(eps_vals) - 1, 0, -1):
                if eps_vals[i] > eps_vals[i - 1] and eps_vals[i - 1] > 0:
                    consec += 1
                else:
                    break
            if consec >= 5:
                score += 3
                detail["연속어닝비트"] = f"{consec}년 연속 EPS 성장"
            elif consec >= 4:
                score += 2.5
                detail["연속어닝비트"] = f"{consec}년 연속 EPS 성장"
            elif consec >= 3:
                score += 2
                detail["연속어닝비트"] = f"{consec}년 연속 EPS 성장"
            elif consec == 2:
                score += 1.5
                detail["연속어닝비트"] = f"{consec}년 연속 EPS 성장"
            elif consec == 1:
                score += 0.5
                detail["연속어닝비트"] = f"{consec}년 EPS 성장"
            else:
                detail["연속어닝비트"] = "EPS 감소"
        else:
            detail["최근서프라이즈"] = "N/A"
            detail["연속어닝비트"]   = "N/A"

        # 8) 포워드 컨센서스 성장률 (+2/+1.5/+1/+0.5/-0.5)
        if eps_forward and eps_vals:
            fwd_eps = eps_forward[1]
            act_eps = eps_vals[-1]
            if act_eps > 0 and fwd_eps > 0:
                fwd_g = (fwd_eps - act_eps) / act_eps
                if fwd_g > 1.00:
                    score += 2
                    detail["EPS추세"] = f"컨센서스 포워드 +{fwd_g*100:.0f}% (폭발)"
                elif fwd_g > 0.50:
                    score += 1.5
                    detail["EPS추세"] = f"컨센서스 포워드 +{fwd_g*100:.0f}% (강)"
                elif fwd_g > 0.20:
                    score += 1
                    detail["EPS추세"] = f"컨센서스 포워드 +{fwd_g*100:.0f}%"
                elif fwd_g > 0:
                    score += 0.5
                    detail["EPS추세"] = f"컨센서스 포워드 +{fwd_g*100:.0f}% (소)"
                else:
                    score -= 0.5
                    detail["EPS추세"] = f"컨센서스 포워드 {fwd_g*100:.0f}% (하락예상)"
            else:
                detail["EPS추세"] = "N/A"
        elif len(eps_vals) >= 3:
            # 포워드 추정치 없으면 과거 3년 추세로 대체
            recent3 = eps_vals[-3:]
            rising = all(recent3[i] < recent3[i + 1] for i in range(len(recent3) - 1))
            if rising and recent3[-1] > 0:
                score += 1
                detail["EPS추세"] = f"3년 연속 EPS 상승 ({recent3[0]:.0f}→{recent3[-1]:.0f})"
            else:
                detail["EPS추세"] = "N/A"
        else:
            detail["EPS추세"] = "N/A"

    except Exception:
        detail.setdefault("최근서프라이즈", "N/A")
        detail.setdefault("연속어닝비트",   "N/A")
        detail.setdefault("EPS추세",        "N/A")

    return min(score, 27.5), detail


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
        # tech < TECH_MIN_GATE → 하락추세 종목 강제 제외
        if t_score < TECH_MIN_GATE:
            total = (t_score / TECH_MAX) * W_TECH * 10  # 재무 반영 안 함 (패널티만)
        else:
            total = (t_score / TECH_MAX) * W_TECH * 10 + (f_score / FUND_MAX) * W_FUND * 10
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
            print(f"  분석 중... {done}/{total_syms}  {sym}", flush=True)
            try:
                results.append(fut.result())
            except Exception:
                pass
    print(f"  분석 완료! ({total_syms}개)")

    # 섹터/산업 필터 적용
    if sector_filter:
        before = len(results)

        def _sector_match(val: str) -> bool:
            if not val:
                return False
            # 정확 일치 또는 부분 포함 ("반도체" in "반도체와반도체장비")
            return val in sector_filter or any(f in val for f in sector_filter)

        def _result_sector_match(r: dict) -> bool:
            kor_s = r.get("sector",   "")
            kor_i = r.get("industry", "")
            # 한글 업종이 있으면 그것만 사용 (yfinance 영문 분류 무시)
            # → 지주사/복합기업이 반도체 자회사 때문에 오분류되는 문제 방지
            if kor_s or kor_i:
                return _sector_match(kor_s) or _sector_match(kor_i)
            # 한글 업종 없으면 yfinance 영문으로 fallback
            fd = r.get("f_detail", {})
            return (_sector_match(fd.get("sector_en",   ""))
                    or _sector_match(fd.get("industry_en", "")))

        results = [r for r in results if _result_sector_match(r)]
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
        # 기술 상세: 모든 종목 출력
        if r["t_detail"] and "error" not in r["t_detail"]:
            td = r["t_detail"]
            print(f"       기술: SMA20={td.get('SMA20','-')}  RSI={td.get('RSI14','-')}  "
                  f"ROC20={td.get('ROC20','-')}  거래량={td.get('거래량','-')}")
        # 재무 상세: 모든 종목 출력 (N/A·오류 진단용)
        if use_fundamental:
            fd = r.get("f_detail") or {}
            dart_err = fd.get("DART오류") or fd.get("DART")
            err_str  = f"  ⚠DART={dart_err}" if dart_err else ""
            print(f"       재무: PER={fd.get('PER','-')}  ROE={fd.get('ROE','-')}  "
                  f"매출성장={fd.get('매출성장','-')}  이익성장={fd.get('이익성장','-')}  "
                  f"부채={fd.get('부채비율','-')}{err_str}")
            print(f"       실적: 분기매출={fd.get('분기매출YoY','-')}  분기순이익={fd.get('분기순이익YoY','-')}  "
                  f"서프라이즈={fd.get('최근서프라이즈','-')}  "
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

    import unicodedata as _ucd
    def _sep(text, width=80):
        dw = sum(2 if _ucd.east_asian_width(c) in ('W', 'F') else 1 for c in text)
        p = max(0, (width - dw) // 2)
        return f"{'━'*p}{text}{'━'*(width - p - dw)}"
    _txt = f" 종목 스크리너  [{datetime.now().strftime('%Y-%m-%d %H:%M')}]  모드: {args.mode.upper()} "
    print(f"\n{_sep(_txt)}")

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
        print(f"\n  선별 {top_n}개: {', '.join(selected)}")

    print()


if __name__ == "__main__":
    main()
