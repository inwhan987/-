# -*- coding: utf-8 -*-
"""pykrx 기반 데이터 수집 + 로컬 캐시.

중요:
- 유니버스는 '과거 시점'의 상장 종목 리스트에서 뽑는다 → 상장폐지 종목 포함 (생존편향 차단)
- 가격은 반드시 adjusted=True (수정주가) — 액면분할·무상증자 가짜 급락 차단
- 모든 다운로드는 종목 단위로 캐시 → 중단 후 재실행하면 이어받는다
"""
from __future__ import annotations

import os
import sys
import time
import pickle
import threading
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from .config import CACHE_DIR, UniverseCfg

warnings.filterwarnings("ignore")

_SLEEP = 0.12          # KRX 레이트리밋 회피
_RETRY = 3


class OfflineCacheMiss(RuntimeError):
    """오프라인 모드인데 캐시에 없는 데이터를 요청했을 때."""


def set_offline(flag: bool = True) -> None:
    """네트워크를 아예 막는다. 캐시에 없으면 조용히 넘어가지 않고 예외를 낸다.

    KRX가 차단된 환경(샌드박스 등)에서 반쪽짜리 데이터로 백테스트가 도는
    사고를 막기 위한 것. 수집은 네트워크 되는 곳에서 한 번 하고, 분석은
    그 캐시로 돌리면 된다.
    """
    global _OFFLINE
    _OFFLINE = bool(flag)


_OFFLINE = False


def _lazy_stock():
    if _OFFLINE:
        raise OfflineCacheMiss(
            "오프라인 모드: 캐시에 없는 데이터를 요청했습니다. "
            "네트워크가 되는 환경에서 먼저 수집하세요.")
    try:
        from pykrx import stock
    except ImportError:  # pragma: no cover
        raise SystemExit("pykrx가 필요합니다:  pip install pykrx")
    return stock


def _cache_path(*parts: str) -> Path:
    p = CACHE_DIR.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load(path: Path):
    if path.exists():
        try:
            with open(path, "rb") as f:
                return pickle.load(f)
        except Exception:
            return None
    return None


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(obj, f, protocol=4)
    tmp.replace(path)


class Blocked(RuntimeError):
    """KRX가 요청을 거부/차단한 것으로 보일 때. 캐시에 남기지 않고 즉시 멈춘다."""


class _RateLimiter:
    """스레드 공용 토큰 버킷. 병렬로 받아도 전체 초당 요청 수가 지켜진다.

    호출마다 sleep 하는 방식은 병렬에서 무력하다(워커 4개면 4배로 나간다).
    KRX는 과도한 요청에 IP를 하루 단위로 막으므로 보수적으로 간다.
    """

    def __init__(self, rps: float):
        self.interval = 1.0 / max(rps, 0.05)
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait < 0:
                wait = 0.0
                self._next = now
            self._next += self.interval
        if wait > 0:
            time.sleep(wait)


_RPS = float(os.environ.get("BT_SWING_RPS", "2.0"))
_limiter = _RateLimiter(_RPS)


def set_rps(rps: float) -> None:
    """초당 총 요청 수 상한을 바꾼다. 차단당한 적 있으면 1.0 이하로."""
    global _limiter
    _limiter = _RateLimiter(rps)

# 차단으로 보이는 에러 신호
_BLOCK_HINTS = ("429", "403", "too many", "blocked", "차단", "제한",
                "forbidden", "connection", "timed out", "timeout",
                "remote end closed", "expecting value")


def _looks_blocked(e: BaseException) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return any(h in s for h in _BLOCK_HINTS)


def _retry(fn, *args, **kwargs):
    last = None
    for i in range(_RETRY):
        _limiter.acquire()
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            if _looks_blocked(e):
                time.sleep(2.0 * (i + 1))
            else:
                time.sleep(0.5 * (i + 1))
    if last is not None and _looks_blocked(last):
        raise Blocked(f"{type(last).__name__}: {str(last)[:120]}")
    raise last  # type: ignore[misc]


# ── 영업일 ────────────────────────────────────────────────────────────
def trading_days(start: str, end: str) -> list[str]:
    """KOSPI 지수 일봉으로 영업일 리스트를 만든다."""
    cache = _cache_path("meta", f"tdays_{start}_{end}.pkl")
    hit = _load(cache)
    if hit:
        return hit
    stock = _lazy_stock()
    idx = _retry(stock.get_index_ohlcv, start, end, "1001")
    days = [d.strftime("%Y%m%d") for d in idx.index]
    _save(cache, days)
    return days


def index_ohlcv(start: str, end: str, code: str = "1001") -> pd.DataFrame:
    cache = _cache_path("meta", f"index_{code}_{start}_{end}.pkl")
    hit = _load(cache)
    if hit is not None:
        return hit
    stock = _lazy_stock()
    df = _retry(stock.get_index_ohlcv, start, end, code)
    df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                            "종가": "close", "거래량": "volume"})
    df.index = pd.to_datetime(df.index)
    _save(cache, df)
    return df


# ── 유니버스 ──────────────────────────────────────────────────────────
def _snapshot_dates(days: list[str], every: int = 60) -> list[str]:
    """분기(약 60영업일) 간격 스냅샷 날짜."""
    if not days:
        return []
    picks = days[::every]
    if days[-1] not in picks:
        picks.append(days[-1])
    return picks


def build_universe(cfg: UniverseCfg, verbose: bool = True) -> pd.DataFrame:
    """과거 시점 스냅샷을 훑어 후보 종목을 뽑는다.

    반환: index=ticker, columns=[name, market, max_value_eok, first_seen, last_seen]
    상장폐지된 종목도 과거 스냅샷에 잡히므로 그대로 포함된다.
    """
    cache = _cache_path("meta", f"universe_{cfg.start}_{cfg.end}_{cfg.min_value_eok:g}.pkl")
    hit = _load(cache)
    if hit is not None:
        if cfg.max_tickers:
            hit = hit.head(cfg.max_tickers)
        return hit

    stock = _lazy_stock()
    days = trading_days(cfg.start, cfg.end)
    snaps = _snapshot_dates(days)
    rows: dict[str, dict] = {}

    for i, d in enumerate(snaps):
        if verbose:
            print(f"  [유니버스] {d}  ({i+1}/{len(snaps)})", flush=True)
        for mkt in cfg.markets:
            try:
                df = _retry(stock.get_market_ohlcv_by_ticker, d, market=mkt)
            except Exception as e:  # noqa: BLE001
                print(f"    스냅샷 실패 {d}/{mkt}: {str(e)[:60]}")
                continue
            if df is None or df.empty:
                continue
            if "거래대금" not in df.columns:
                continue
            val_eok = df["거래대금"] / 1e8
            close = df["종가"]
            ok = (val_eok >= cfg.min_value_eok) & (close >= cfg.min_price) & (close <= cfg.max_price)
            for tk in df.index[ok]:
                # first_value_eok = '처음 후보로 잡힌 시점'의 거래대금.
                # max_value_eok(기간 전체 최대치)로 정렬해 상위를 자르면
                # 나중에 거래대금이 터질 종목을 미리 아는 셈이라 미래참조가 된다.
                r = rows.setdefault(tk, {"market": mkt, "max_value_eok": 0.0,
                                         "first_value_eok": float(val_eok.loc[tk]),
                                         "first_seen": d, "last_seen": d})
                r["max_value_eok"] = max(r["max_value_eok"], float(val_eok.loc[tk]))
                r["last_seen"] = d

    if not rows:
        raise SystemExit("유니버스가 비었습니다. 기간/거래대금 기준을 확인하세요.")

    uni = pd.DataFrame.from_dict(rows, orient="index")
    uni.index.name = "ticker"

    # 우선주(끝자리 0이 아님) / 스팩 제외
    if cfg.exclude_preferred:
        uni = uni[[t.endswith("0") for t in uni.index]]
    names = {}
    for tk in uni.index:
        try:
            names[tk] = stock.get_market_ticker_name(tk)
        except Exception:  # noqa: BLE001
            names[tk] = ""
    uni["name"] = pd.Series(names)
    if cfg.exclude_spac:
        uni = uni[~uni["name"].astype(str).str.contains("스팩", na=False)]

    # 미래 정보가 안 들어가도록 '최초 관측 시점' 거래대금으로 정렬
    uni = uni.sort_values("first_value_eok", ascending=False)
    _save(cache, uni)
    if verbose:
        print(f"  [유니버스] 후보 {len(uni)}종목")
    if cfg.max_tickers:
        uni = uni.head(cfg.max_tickers)
    return uni


# ── 종목별 시계열 ──────────────────────────────────────────────────────
def ohlcv(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    """수정주가 일봉. 실패 시 None."""
    cache = _cache_path("ohlcv", f"{ticker}_{start}_{end}.pkl")
    hit = _load(cache)
    if hit is not None:
        return None if isinstance(hit, str) else hit

    stock = _lazy_stock()
    try:
        df = _retry(stock.get_market_ohlcv, start, end, ticker, adjusted=True)
    except Blocked:
        raise                      # 차단은 캐시에 남기지 않고 위로 던진다
    except Exception:  # noqa: BLE001
        return None                # 일시적 실패도 캐시하지 않는다 → 다음에 재시도
    if df is None or df.empty:
        _save(cache, "ERR:empty")
        return None

    df = df.rename(columns={"시가": "open", "고가": "high", "저가": "low",
                            "종가": "close", "거래량": "volume", "거래대금": "value",
                            "등락률": "chg"})
    keep = [c for c in ["open", "high", "low", "close", "volume", "value"] if c in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    # 거래정지 등으로 0인 봉 제거
    df = df[(df["close"] > 0) & (df["open"] > 0)]
    if "value" not in df.columns:
        df["value"] = df["close"] * df["volume"]
    if len(df) < 60:
        _save(cache, "ERR:too_short")
        return None
    _save(cache, df)
    return df


def flow(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    """투자자별 순매수 거래대금 (기관/외국인/개인). 실패 시 None."""
    cache = _cache_path("flow", f"{ticker}_{start}_{end}.pkl")
    hit = _load(cache)
    if hit is not None:
        return None if isinstance(hit, str) else hit

    stock = _lazy_stock()
    try:
        df = _retry(stock.get_market_trading_value_by_date, start, end, ticker)
    except Blocked:
        raise
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        _save(cache, "ERR:empty")
        return None

    ren = {"기관합계": "inst", "외국인합계": "forgn", "개인": "indiv",
           "기타법인": "corp", "전체": "total"}
    df = df.rename(columns=ren)
    keep = [c for c in ["inst", "forgn", "indiv"] if c in df.columns]
    if not keep:
        _save(cache, "ERR:no_cols")
        return None
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    _save(cache, df)
    return df


def market_cap(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    """시가총액·상장주식수. 시총이 너무 작아 휘둘리는 종목을 거르는 데 쓴다."""
    cache = _cache_path("cap", f"{ticker}_{start}_{end}.pkl")
    hit = _load(cache)
    if hit is not None:
        return None if isinstance(hit, str) else hit

    stock = _lazy_stock()
    try:
        df = _retry(stock.get_market_cap, start, end, ticker)
    except Blocked:
        raise
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        _save(cache, "ERR:empty")
        return None
    df = df.rename(columns={"시가총액": "mktcap", "상장주식수": "shares"})
    keep = [c for c in ["mktcap", "shares"] if c in df.columns]
    if not keep:
        _save(cache, "ERR:no_cols")
        return None
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    _save(cache, df)
    return df


def fundamental(ticker: str, start: str, end: str) -> pd.DataFrame | None:
    """PER/PBR/EPS/BPS/DIV. KRX 공표치라 발표 시점 이후 값만 들어있다."""
    cache = _cache_path("fund", f"{ticker}_{start}_{end}.pkl")
    hit = _load(cache)
    if hit is not None:
        return None if isinstance(hit, str) else hit

    stock = _lazy_stock()
    try:
        df = _retry(stock.get_market_fundamental, start, end, ticker)
    except Blocked:
        raise
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        _save(cache, "ERR:empty")
        return None
    df = df.rename(columns=str.upper)
    keep = [c for c in ["PER", "PBR", "EPS", "BPS", "DIV"] if c in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    _save(cache, df)
    return df


# ── 일괄 로드 ─────────────────────────────────────────────────────────
def purge_error_cache(verbose: bool = True) -> int:
    """실패로 기록된 캐시 항목을 지운다.

    IP 차단을 당한 뒤에는 그 시간대에 요청된 종목이 전부 실패로 캐시돼,
    차단이 풀려도 영영 건너뛰게 된다. 차단 후에는 반드시 한 번 돌릴 것.
    """
    n = 0
    for sub in ("ohlcv", "flow", "fund", "cap"):
        d = CACHE_DIR / sub
        if not d.exists():
            continue
        for f in d.glob("*.pkl"):
            obj = _load(f)
            if isinstance(obj, str):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
    if verbose:
        print(f"  [캐시정리] 실패 기록 {n}건 삭제 — 다음 실행에서 다시 받습니다")
    return n


def _load_one(tk: str, start: str, end: str, with_flow: bool, with_fund: bool,
              with_cap: bool = True) -> tuple[str, pd.DataFrame | None]:
    px = ohlcv(tk, start, end)
    if px is None:
        return tk, None
    df = px
    if with_flow:
        fl = flow(tk, start, end)
        if fl is not None:
            df = df.join(fl, how="left")
    for c in ("inst", "forgn", "indiv"):
        if c not in df.columns:
            df[c] = np.nan
    if with_fund:
        fu = fundamental(tk, start, end)
        if fu is not None:
            df = df.join(fu, how="left")
    for c in ("PER", "PBR", "EPS", "BPS", "DIV"):
        if c not in df.columns:
            df[c] = np.nan
    if with_cap:
        cp = market_cap(tk, start, end)
        if cp is not None:
            df = df.join(cp, how="left")
    for c in ("mktcap", "shares"):
        if c not in df.columns:
            df[c] = np.nan
    return tk, df.sort_index()


LAST_LOAD: dict = {"done": 0, "n": 0, "ok": 0, "expired": False}


def load_panel(
    tickers: list[str],
    start: str,
    end: str,
    with_flow: bool = True,
    with_fund: bool = True,
    with_cap: bool = True,
    verbose: bool = True,
    workers: int = 1,
    budget_sec: float = 0.0,
) -> dict[str, pd.DataFrame]:
    """종목별 DataFrame 딕셔너리. 가격 + 수급 + 펀더멘털을 날짜 기준으로 합친다.

    workers > 1이면 병렬로 받는다. 종목마다 캐시 파일이 따로라 충돌은 없지만,
    KRX가 과도한 동시 요청을 차단할 수 있으니 3~4를 넘기지 않는 게 안전하다.
    """
    out: dict[str, pd.DataFrame] = {}
    n = len(tickers)
    t0 = time.time()
    done = 0
    miss = [0]

    def _expired() -> bool:
        return budget_sec > 0 and (time.time() - t0) > budget_sec

    def _guard():
        # 연속으로 대량 실패하면 차단일 가능성이 높다. 1000종목을 끝까지
        # 두드리며 캐시를 오염시키느니 일찍 멈추는 게 낫다.
        if done >= 40 and miss[0] >= max(30, int(done * 0.8)):
            raise Blocked(
                f"{done}종목 중 {miss[0]}종목 실패 — 차단으로 판단하고 중단합니다")

    def _tick():
        nonlocal done
        done += 1
        if verbose and (done % 25 == 0 or done == n):
            el = time.time() - t0
            eta = el / done * (n - done)
            print(f"  [로드] {done}/{n}  성공 {len(out)}  경과 {el/60:.1f}분  "
                  f"남은시간 ~{eta/60:.1f}분", flush=True)

    if workers and workers > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_load_one, tk, start, end, with_flow, with_fund, with_cap): tk
                    for tk in tickers}
            try:
                for fu in as_completed(futs):
                    try:
                        tk, df = fu.result()
                        if df is not None:
                            out[tk] = df
                        else:
                            miss[0] += 1
                    except Blocked:
                        raise
                    except Exception as e:  # noqa: BLE001
                        miss[0] += 1
                        print(f"  로드 실패 {futs[fu]}: {str(e)[:60]}")
                    _tick()
                    _guard()
            except Blocked as e:
                for f in futs:
                    f.cancel()
                raise
    else:
        for tk in tickers:
            if _expired():
                if verbose:
                    print(f"  [로드] 시간 예산 소진 — {done}/{n}까지 받고 중단합니다")
                break
            try:
                _tk, df = _load_one(tk, start, end, with_flow, with_fund, with_cap)
                if df is not None:
                    out[_tk] = df
                else:
                    miss[0] += 1
            except Blocked:
                raise
            except Exception as e:  # noqa: BLE001
                miss[0] += 1
                print(f"  로드 실패 {tk}: {str(e)[:60]}")
            _tick()
            _guard()

    # --download-only 가 "다 받았는지" 판단할 수 있도록 마지막 로드 통계를 남긴다.
    LAST_LOAD.update(done=done, n=n, ok=len(out), expired=_expired() and done < n)
    if verbose:
        print(f"  [로드] 완료 {len(out)}/{n}종목  ({(time.time()-t0)/60:.1f}분)")
    return out
