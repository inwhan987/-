"""한국투자증권 KIS OpenAPI 클라이언트.

참고 문서: https://apiportal.koreainvestment.com/apiservice
TR ID 는 모의투자(paper) / 실전(real)에서 다르므로 분기 처리한다.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from stock_bot.config import settings

try:
    import fcntl  # Linux 전용. 두 봇 프로세스 간 유량 조율(파일락)에 사용.
except ImportError:  # Windows 로컬/테스트 → 프로세스 내부 게이트로 폴백
    fcntl = None

# 환경별로 토큰이 다르므로 paper/real 분리 캐시
TOKEN_CACHE_DIR = Path(".kis_tokens")


@dataclass
class Quote:
    symbol: str
    price: float
    change_pct: float


class OrderRejectedError(RuntimeError):
    """KIS 주문이 거부됨 (rt_cd != 0). 영업일 아님·증거금 부족 등."""


class KISBroker:
    def __init__(self) -> None:
        self.base_url = settings.kis_base_url
        self.app_key = settings.kis_app_key
        self.app_secret = settings.kis_app_secret
        self.account_no = settings.kis_account_no
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(base_url=self.base_url, timeout=30.0)
        # 분봉 캐시: symbol → 마지막 정상 응답 데이터 (5xx 완전 실패 시 폴백용)
        self._minute_ohlcv_cache: dict[str, list] = {}
        # 오늘 분봉(페이지네이션+resample) 캐시: "symbol_interval" → (bar_key, result)
        self._minute_today_cache: dict[str, tuple[Any, list]] = {}
        # 증분 캐싱용 원본 1분봉 누적: symbol → (날짜, {time: bar}).
        # 확정된 과거 1분봉은 다시 안 받고, 최신 구간만 받아 덮어쓴다.
        self._minute_raw_accum: dict[str, tuple[Any, dict[str, dict[str, Any]]]] = {}
        # 능동 유량 게이트(RateLimiter): 호출이 한도에 걸리기 전 간격을 띄운다.
        self._req_lock = threading.Lock()
        # priority=True 호출(청산 손절/익절 체크)이 대기 중인 일반 호출보다
        # 먼저 게이트를 통과하도록 하는 프로세스 내 우선순위 큐(2026-08-15).
        self._req_cond = threading.Condition(self._req_lock)
        self._priority_waiting = 0
        self._throttle_busy = False
        self._last_req_ts: float = 0.0
        # 프로세스 간 유량 조율용 공유 락 파일 fd. 두 봇이 같은 앱키를 쓰므로
        # 이 파일의 '마지막 호출 시각'을 flock 으로 상호배제해 합산 한도를 지킨다.
        self._gate_fd: int | None = self._open_gate_fd()
        # 개장일 캐시: "YYYYMMDD" → 개장 여부(True/False). 일 1회 조회.
        self._holiday_cache: dict[str, bool] = {}
        # 휴장일 API가 데이터를 못 준 날짜(모의 도메인 미지원 등) → 재조회 안 함
        self._holiday_unavailable: set[str] = set()

    @staticmethod
    def _code(symbol: str) -> str:
        """'005930.KS' → '005930' (KIS API는 6자리 코드만 허용)."""
        return symbol.split(".")[0]

    # ---------- Auth ----------
    @property
    def _token_cache_path(self) -> Path:
        return TOKEN_CACHE_DIR / f"{settings.kis_env}.json"

    def _load_cached_token(self) -> None:
        """디스크 캐시에서 유효 토큰 로드. 실패/만료 시 무시."""
        p = self._token_cache_path
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("app_key") != self.app_key:
                return  # 키가 바뀌면 캐시 무효
            if time.time() < float(data.get("expires_at", 0)) - 300:
                self._token = data["access_token"]
                self._token_expires_at = float(data["expires_at"])
        except Exception as exc:
            logger.debug("token cache load failed: {}", exc)

    def _save_token_cache(self) -> None:
        try:
            TOKEN_CACHE_DIR.mkdir(exist_ok=True)
            self._token_cache_path.write_text(
                json.dumps(
                    {
                        "app_key": self.app_key,
                        "access_token": self._token,
                        "expires_at": self._token_expires_at,
                    }
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.debug("token cache save failed: {}", exc)

    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        # 인스턴스마다 재생성되는 경우를 위해 디스크 캐시 먼저 확인
        self._load_cached_token()
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = self._client.post(
            "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + int(data.get("expires_in", 86400))
        self._save_token_cache()
        logger.info("KIS access token issued (env={}, cached)", settings.kis_env)
        return self._token

    def _headers(self, tr_id: str) -> dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._ensure_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }

    def _open_gate_fd(self) -> int | None:
        """프로세스 간 공유 게이트 파일 fd 를 연다(없으면 None → 내부 게이트만)."""
        if fcntl is None:
            return None  # Windows: 파일락 미지원 → 내부 게이트로 폴백
        path = settings.kis_gate_path
        if not path:
            return None
        try:
            return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            logger.warning("KIS 유량 게이트 파일 열기 실패({}) — 내부 게이트만 사용", exc)
            return None

    def _throttle(self, priority: bool = False) -> None:
        """능동 유량 게이트. 마지막 호출로부터 최소 간격(1/한도초)이 지나도록 대기.

        한도에 '걸린 뒤 백오프'가 아니라 '걸리기 전에' 호출을 평탄화한다.
        공유 락 파일이 있으면 두 봇(별 프로세스) 합산까지 같은 키 한도로 조율하고,
        없으면(Windows 등) 프로세스 내부 간격만 강제한다.

        priority=True(청산 손절/익절 시세조회 등)는 같은 프로세스 내에서 대기 중인
        일반 호출보다 먼저 게이트를 통과한다(2026-08-15). 프로세스 간(다른 봇)
        우선순위까지는 보장 못 함 — flock 순서는 커널이 정함. 다만 청산체크가
        차지하는 호출량 자체가 작아(5초 주기) 그 영향은 제한적.
        """
        rate = settings.kis_rate_limit
        if rate <= 0:
            return
        min_interval = 1.0 / rate
        with self._req_cond:
            if priority:
                self._priority_waiting += 1
            try:
                while self._throttle_busy or (not priority and self._priority_waiting > 0):
                    self._req_cond.wait(timeout=0.5)
                self._throttle_busy = True
            finally:
                if priority:
                    self._priority_waiting -= 1
        try:
            if self._gate_fd is not None:
                self._throttle_cross(min_interval)
            else:
                wait = self._last_req_ts + min_interval - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                self._last_req_ts = time.monotonic()
        finally:
            with self._req_cond:
                self._throttle_busy = False
                self._req_cond.notify_all()

    def _throttle_cross(self, min_interval: float) -> None:
        """공유 파일의 '마지막 호출 시각'을 flock 으로 상호배제하며 간격을 강제.

        두 컨테이너가 같은 호스트 파일(같은 inode)을 보므로 한쪽이 락+sleep 하는
        동안 다른 쪽은 대기 → 합산 호출이 키 한도를 넘지 않는다. 벽시계(time.time)
        를 써야 프로세스 간 비교가 가능하다(monotonic 은 프로세스별로 기준이 다름).
        """
        fd = self._gate_fd
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 64).strip()
                last = float(raw) if raw else 0.0
                now = time.time()
                wait = last + min_interval - now
                if 0 < wait <= 5:  # 비정상적으로 큰 대기는 무시(시계 역행/손상 방지)
                    time.sleep(wait)
                    now = time.time()
                payload = f"{now:.6f}".encode()
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, payload)
                os.ftruncate(fd, len(payload))
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError as exc:
            # 파일락 실패 시 내부 게이트로 폴백(틱을 죽이지 않음)
            logger.warning("KIS 유량 게이트 파일락 실패({}) — 내부 간격으로 폴백", exc)
            wait = self._last_req_ts + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_req_ts = time.monotonic()

    def _get_with_retry(
        self, path: str, tr_id: str, params: dict[str, Any], *, label: str = "",
        attempts: int = 5, priority: bool = False,
    ) -> httpx.Response:
        """KIS GET + 5xx 지수백오프 재시도. 모의서버의 간헐적 500 을 흡수.

        재시도마다 게이트(_throttle)를 다시 통과해 재시도 폭주도 억제한다.
        """
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                self._throttle(priority=priority)
                resp = self._client.get(path, headers=self._headers(tr_id), params=params)
                resp.raise_for_status()
                return resp
            except httpx.RemoteProtocolError as exc:
                # KIS 서버가 keep-alive 연결을 닫은 후 재사용 시 발생 → 클라이언트 재생성 후 재시도
                last_exc = exc
                if attempt == attempts - 1:
                    logger.warning(
                        "KIS {} 연결 끊김 (RemoteProtocolError) — {}회 재시도 모두 실패",
                        label or path, attempts - 1,
                    )
                    raise
                # 중간 재시도는 대부분 곧 복구되므로 로그 미출력 (최종 실패에만 WARNING)
                try:
                    self._client.close()  # 미close 시 keep-alive 소켓 fd 누수 (Errno 24)
                except Exception:
                    pass
                self._client = httpx.Client(base_url=self.base_url, timeout=30.0)
                time.sleep(0.5)
            except httpx.TimeoutException as exc:
                # 읽기/연결 타임아웃 (KIS 모의서버 간헐 지연) → 지수백오프 재시도.
                # GET 시세조회는 멱등이라 재시도 안전. 미재시도 시 단발 지연이
                # 곧장 틱 실패로 이어졌음(예: get_minute_ohlcv_today ReadTimeout).
                last_exc = exc
                if attempt == attempts - 1:
                    logger.warning(
                        "KIS {} 타임아웃 — {}회 재시도 모두 실패",
                        label or path, attempts - 1,
                    )
                    raise
                time.sleep(1.0 * (2 ** attempt))
            except httpx.NetworkError as exc:
                # DNS 조회 실패/연결 거부 등 (예: "No address associated with hostname").
                # 서버에 닿기도 전에 끊긴 일시적 망 장애라 잠시 뒤 재시도하면 대부분 복구.
                # 미재시도 시 단발 DNS 끊김이 틱 전체를 중단시켰음.
                last_exc = exc
                if attempt == attempts - 1:
                    logger.warning(
                        "KIS {} 연결/DNS 실패 ({}) — {}회 재시도 모두 실패",
                        label or path, type(exc).__name__, attempts - 1,
                    )
                    raise
                time.sleep(1.0 * (2 ** attempt))
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                code = exc.response.status_code
                if code < 500:
                    raise  # 4xx: 비재시도 → 호출측에서 처리
                if attempt == attempts - 1:
                    logger.warning(
                        "KIS {} returned {} — {}회 재시도 모두 실패",
                        label or path, code, attempts - 1,
                    )
                    raise
                wait = 1.5 * (2 ** attempt)
                # 모의서버 간헐 500 은 보통 재시도로 흡수되므로 로그 미출력
                time.sleep(wait)
        if last_exc:
            raise last_exc
        raise RuntimeError("unreachable")

    # ---------- Market data ----------
    def get_quote(self, symbol: str, *, priority: bool = False) -> Quote:
        """현재가 조회 (국내주식 현재가). priority=True 는 유량 게이트 1순위(청산 체크용)."""
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": self._code(symbol)}
        resp = self._get_with_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100", params, label=f"quote {symbol}", priority=priority,
        )
        output = resp.json()["output"]
        return Quote(
            symbol=symbol,
            price=float(output["stck_prpr"]),
            change_pct=float(output["prdy_ctrt"]),
        )

    def get_index_quote(self, code: str = "0001") -> dict[str, float]:
        """국내 업종지수 현재가 조회.

        code: 0001=코스피, 1001=코스닥, 2001=코스피200.
        반환: {"price": 현재지수, "change_pct": 전일대비율}
        실패 시 호출측에서 best-effort 처리(try/except) 가정.
        """
        params = {
            "FID_COND_MRKT_DIV_CODE": "U",
            "FID_INPUT_ISCD": code,
        }
        resp = self._get_with_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            "FHPUP02100000", params, label=f"index {code}",
        )
        output = resp.json().get("output", {}) or {}
        return {
            "price": float(output.get("bstp_nmix_prpr") or 0),
            "change_pct": float(output.get("bstp_nmix_prdy_ctrt") or 0),
        }

    def get_minute_ohlcv(
        self, symbol: str, interval_min: int = 5, count: int = 120
    ) -> list[dict[str, Any]]:
        """당일 분봉 조회 (국내주식 당일분봉).

        interval_min: 1/5/10/30/60 분봉. KIS 는 `FID_INPUT_HOUR_1` 기준 역순 반환.
        5xx 완전 실패 시 이전 캐시 데이터로 폴백해 틱 스킵을 방지한다.
        """
        from datetime import datetime as _dt
        cache_key = f"{symbol}_{interval_min}_{count}"
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": self._code(symbol),
            "FID_INPUT_HOUR_1": _dt.now().strftime("%H%M%S"),
            "FID_PW_DATA_INCU_YN": "N",
        }
        try:
            resp = self._get_with_retry(
                "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
                "FHKST03010200", params, label=f"minute {symbol}",
            )
        except httpx.HTTPStatusError as exc:
            # 5회 재시도 후에도 5xx → 이전 캐시 반환 (틱 스킵 방지)
            if exc.response.status_code >= 500 and cache_key in self._minute_ohlcv_cache:
                logger.warning(
                    "KIS minute OHLCV 완전 실패 ({}), 이전 캐시 데이터로 대체",
                    symbol,
                )
                return self._minute_ohlcv_cache[cache_key]
            raise
        rows = resp.json().get("output2", [])[:count]
        result = [
            {
                "date": r.get("stck_bsop_date", ""),   # 영업일자 (YYYYMMDD)
                "time": r.get("stck_cntg_hour") or r.get("stck_bsop_hour"),
                "open": float(r["stck_oprc"]),
                "high": float(r["stck_hgpr"]),
                "low": float(r["stck_lwpr"]),
                "close": float(r["stck_prpr"]),
                "volume": int(r.get("cntg_vol", 0) or 0),
            }
            for r in rows
            if r.get("stck_prpr")
        ]
        if result:
            self._minute_ohlcv_cache[cache_key] = result
        return result

    # ---------- 오늘 분봉 페이지네이션 (1분 → N분 실 OHLC) ----------
    _MINUTE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"

    def _fetch_minute_page(self, code: str, hour1: str) -> list[dict[str, Any]]:
        """hour1(HHMMSS) 기준 과거 30개 1분봉 1페이지. 실패 시 []."""
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": hour1,
            "FID_PW_DATA_INCU_YN": "N",
        }
        try:
            resp = self._get_with_retry(
                self._MINUTE_PATH, "FHKST03010200", params, label=f"minute-page {code}",
            )
        except httpx.HTTPStatusError:
            return []
        rows = resp.json().get("output2", [])
        out: list[dict[str, Any]] = []
        for r in rows:
            t = r.get("stck_cntg_hour") or r.get("stck_bsop_hour")
            if not t or not r.get("stck_prpr"):
                continue
            out.append({
                "date": r.get("stck_bsop_date", ""),
                "time": t,
                "open": float(r["stck_oprc"]),
                "high": float(r["stck_hgpr"]),
                "low": float(r["stck_lwpr"]),
                "close": float(r["stck_prpr"]),
                "volume": int(r.get("cntg_vol", 0) or 0),
            })
        return out

    @staticmethod
    def _resample_minute(bars_asc: list[dict[str, Any]], interval: int) -> list[dict[str, Any]]:
        """1분봉(오름차순) → N분봉 실 OHLC, newest-first 반환. origin=start_day(09:00 정렬)."""
        from datetime import datetime as _dt

        import pandas as pd

        if not bars_asc:
            return []
        today = _dt.now().date()
        date_str = today.strftime("%Y%m%d")
        if interval <= 1:
            return list(reversed(bars_asc))
        idx = [_dt.combine(today, _dt.strptime(b["time"], "%H%M%S").time()) for b in bars_asc]
        df = pd.DataFrame(bars_asc, index=pd.DatetimeIndex(idx))
        dfn = (
            df.resample(f"{interval}min", label="left", closed="left", origin="start_day")
            .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
            .dropna(subset=["close"])
        )
        out: list[dict[str, Any]] = []
        for ts, r in dfn.iterrows():
            out.append({
                "date": date_str,
                "time": ts.strftime("%H%M%S"),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": int(r["volume"]),
            })
        out.reverse()  # newest-first (get_minute_ohlcv 와 동일 정렬)
        return out

    def get_minute_ohlcv_today(
        self,
        symbol: str,
        interval_min: int = 5,
        *,
        market_open: str = "090000",
        max_pages: int = 20,
        page_sleep: float = 0.12,
    ) -> list[dict[str, Any]]:
        """오늘 1분봉 전체를 페이지네이션으로 모아 N분봉 실 OHLC 로 변환(newest-first).

        KIS 당일분봉 TR 은 한 호출에 30개·오늘치 1분봉만 준다. `FID_INPUT_HOUR_1`
        을 과거로 옮겨가며 09:00 까지 모은 뒤 N분봉으로 resample 한다.
        같은 N분봉 구간 내 재호출은 캐시(bar_key)로 페이지네이션을 생략한다.

        증분 캐싱: 확정된 과거 1분봉은 종목별로 누적 보관하고, 다음 호출에선
        최신 구간만 받아 덮어쓴다. 최신부터 받다가 '이미 아는 페이지'(new_cnt==0)를
        만나면 멈추므로 후장에도 보통 1~2페이지면 끝난다. 누적분 + 최신 재취득이라
        반환 결과는 매번 09:00~현재 전체를 다시 받았을 때와 동일하다.
        """
        from datetime import datetime as _dt, timedelta as _td

        now = _dt.now()
        if interval_min >= 60:
            bar_key = now.replace(minute=0, second=0, microsecond=0)
        else:
            bar_key = now.replace(
                minute=(now.minute // interval_min) * interval_min,
                second=0, microsecond=0,
            )
        ck = f"{symbol}_{interval_min}"
        cached = self._minute_today_cache.get(ck)
        if cached and cached[0] == bar_key:
            return cached[1]

        code = self._code(symbol)
        day_key = now.date()
        # 증분: 같은 날 누적해 둔 1분봉이 있으면 이어쓰고, 날 바뀌면 새로 시작.
        acc = self._minute_raw_accum.get(symbol)
        if acc and acc[0] == day_key:
            by_time = acc[1]
        else:
            by_time = {}
        anchor = now.strftime("%H%M%S")
        for _ in range(max_pages):
            try:
                rows = self._fetch_minute_page(code, anchor)
            except httpx.HTTPError as exc:
                # 페이지 호출 실패(타임아웃/프로토콜 등). 이미 모은 페이지가 있으면
                # 그걸로 진행(최신부터 모아 현재 봉은 확보됨), 없으면 폴백 경로로.
                logger.warning(
                    "KIS 당일분봉 {} 페이지 실패 — 수집분 {}개로 진행 ({})",
                    code, len(by_time), exc,
                )
                break
            if not rows:
                break
            oldest = min(r["time"] for r in rows)
            # 최신 구간(형성 중 봉 포함)은 항상 덮어써 갱신 → 결과 동일성 유지.
            new_cnt = sum(1 for r in rows if r["time"] not in by_time)
            for r in rows:
                by_time[r["time"]] = r
            # new_cnt==0 = 이번 페이지는 전부 이미 아는 것(증분 종료점).
            if oldest <= market_open or new_cnt == 0:
                break
            nxt = _dt.strptime(oldest, "%H%M%S") - _td(minutes=1)
            anchor = nxt.strftime("%H%M%S")
            # 호출 간격은 _throttle(유량 게이트)가 책임지므로 별도 sleep 불필요.

        # 누적분 보관(같은 dict 객체를 계속 사용). 날 바뀐 경우 새 dict 로 갱신.
        if by_time:
            self._minute_raw_accum[symbol] = (day_key, by_time)

        if not by_time:
            # 페이지네이션 전부 실패 → 직전 캐시라도 반환
            return cached[1] if cached else []
        bars_asc = [by_time[t] for t in sorted(by_time)]
        result = self._resample_minute(bars_asc, interval_min)
        if result:
            self._minute_today_cache[ck] = (bar_key, result)
        return result

    def get_approval_key(self) -> str:
        """WebSocket 실시간 접속용 승인키 발급."""
        resp = self._client.post(
            "/oauth2/Approval",
            json={
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "secretkey": self.app_secret,
            },
        )
        resp.raise_for_status()
        return resp.json()["approval_key"]

    def get_daily_ohlcv(self, symbol: str, count: int = 100) -> list[dict[str, Any]]:
        """일봉 조회 (최근 count 일).

        구 `inquire-daily-price` 는 30일만 돌려줘서 MACD(35+) 계산이 안 됨.
        신 `inquire-daily-itemchartprice` 는 기간 지정이 가능해 최대 100일치를 받아온다.
        모의서버의 간헐적 5xx 를 위해 최대 3회 재시도.
        """
        from datetime import datetime, timedelta

        end = datetime.now().strftime("%Y%m%d")
        # 주말/공휴일 고려해 여유롭게 1.6배 달력일수
        start = (datetime.now() - timedelta(days=int(count * 1.6) + 10)).strftime("%Y%m%d")
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": self._code(symbol),
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        resp = self._get_with_retry(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100", params, label=f"daily {symbol}",
        )
        # output2 에 일별 배열이 담김 (신 엔드포인트)
        rows = resp.json().get("output2", [])[:count]
        out: list[dict[str, Any]] = []
        for r in rows:
            # 비거래일 빈 행 스킵
            if not r.get("stck_bsop_date") or not r.get("stck_clpr"):
                continue
            out.append({
                "date": r["stck_bsop_date"],
                "open": float(r.get("stck_oprc") or 0),
                "high": float(r.get("stck_hgpr") or 0),
                "low": float(r.get("stck_lwpr") or 0),
                "close": float(r["stck_clpr"]),
                "volume": int(r.get("acml_vol") or 0),
            })
        return out

    # ---------- Orders ----------
    def _order_tr_id(self, side: str) -> str:
        # 모의투자: VTTC, 실전: TTTC. 매수 0802 / 매도 0801.
        prefix = "VTTC" if settings.is_paper else "TTTC"
        suffix = "0802U" if side == "buy" else "0801U"
        return f"{prefix}{suffix}"

    def is_open_day(self, date_str: str | None = None) -> bool:
        """KIS 국내휴장일조회(CTCA0903R) 기준 '개장일' 여부.

        date_str: YYYYMMDD (기본 오늘). 주문 서버와 동일한 KRX 달력을 쓰므로
        exchange_calendars 가 모르는 임시공휴일(선거일 등)까지 정확히 반영된다.
        조회 실패(모의 도메인 미지원 등) 시 예외를 던져 호출측이 폴백하게 한다.
        """
        from datetime import datetime as _dt
        date_str = date_str or _dt.now().strftime("%Y%m%d")
        if date_str in self._holiday_cache:
            return self._holiday_cache[date_str]
        # 모의투자 도메인은 휴장일 API 미지원 → 호출 자체를 스킵(불필요한 500 방지)
        if settings.is_paper:
            raise RuntimeError("KIS 휴장일 API 모의 도메인 미지원")
        # 직전에 데이터 없음으로 확인된 날짜는 재조회 안 함(로그·500 스팸 방지)
        if date_str in self._holiday_unavailable:
            raise RuntimeError(f"KIS 휴장일 정보 없음 (BASS_DT={date_str}, 캐시)")
        params = {"BASS_DT": date_str, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}
        resp = self._get_with_retry(
            "/uapi/domestic-stock/v1/quotations/chk-holiday",
            "CTCA0903R", params, label="holiday", attempts=3,
        )
        rows = resp.json().get("output", []) or []
        opened: bool | None = None
        for row in rows:
            if row.get("bass_dt") == date_str:
                opened = (str(row.get("opnd_yn", "")).strip().upper() == "Y")
                break
        if opened is None:
            self._holiday_unavailable.add(date_str)  # 다음부터 재조회 안 함
            raise RuntimeError(f"KIS 휴장일 정보 없음 (BASS_DT={date_str})")
        self._holiday_cache[date_str] = opened
        return opened

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        price: float = 0.0,
        order_type: str = "market",
    ) -> dict[str, Any]:
        """주식 주문.

        order_type: "market" -> 시장가(01), "limit" -> 지정가(00).
        TRADE_DRY_RUN=true 면 실제 주문을 보내지 않고 로깅만 한다.
        """
        if settings.trade_dry_run:
            logger.warning(
                "[DRY-RUN] would place {} {} x{} @ {}", side, symbol, quantity, price or "market"
            )
            return {"dry_run": True, "side": side, "symbol": symbol, "qty": quantity}

        cano, acnt_prdt = self.account_no.split("-")
        body = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "PDNO": self._code(symbol),
            "ORD_DVSN": "01" if order_type == "market" else "00",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0" if order_type == "market" else str(int(price)),
        }
        import time as _time
        for attempt in range(5):
            self._throttle()
            resp = self._client.post(
                "/uapi/domestic-stock/v1/trading/order-cash",
                headers=self._headers(self._order_tr_id(side)),
                json=body,
            )
            if resp.is_success:
                break
            err = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            # 초당 호출 초과 → 지수 백오프 재시도 (최대 5회: 2,4,6,8,10초)
            if err.get("msg_cd") == "EGW00201" and attempt < 4:
                wait = (attempt + 1) * 2
                logger.warning("KIS 초당 호출 초과, {}초 후 재시도 ({}/5)", wait, attempt + 1)
                _time.sleep(wait)
                continue
            logger.error("order failed: {} {} body={}", resp.status_code, resp.text, body)
            resp.raise_for_status()
        data = resp.json()
        _msg = str(data.get("msg1", "")).strip()
        # rt_cd "0" 만 정상. 그 외(영업일 아님·증거금 부족 등)는 주문 거부 → 예외로 알림.
        if str(data.get("rt_cd", "0")) != "0":
            # 영업일이 아니라는 거부면 오늘을 휴장으로 캐시 → 이후 틱에서 진입 자체를 스킵
            if "영업일" in _msg:
                from datetime import datetime as _dt
                self._holiday_cache[_dt.now().strftime("%Y%m%d")] = False
            logger.error(
                "order rejected: {} {} x{} -> [{}] {}",
                side, symbol, quantity, data.get("msg_cd"), _msg,
            )
            raise OrderRejectedError(f"[{data.get('msg_cd')}] {_msg}")
        logger.info("order: {} {} {} -> {}", side, symbol, quantity, _msg)
        return data

    def get_account_total(self) -> float:
        """계좌 총평가금액 (원). 실패하면 0."""
        summary = self.get_account_summary()
        return summary.get("total_eval", 0.0)

    def get_account_summary(self) -> dict[str, float]:
        """계좌 잔고 요약 (원 단위).

        반환 키:
          - deposit:       예수금 (dnca_tot_amt)
          - stock_eval:    주식 평가금액 (scts_evlu_amt)
          - total_eval:    총 평가금액 (tot_evlu_amt) = 예수금 + 주식 평가
          - purchase:      매입금액 합계 (pchs_amt_smtl_amt)
          - pnl:           평가손익 (evlu_pfls_smtl_amt)
          - pnl_pct:       평가손익률 (%)
        실패 시 모든 값 0.
        """
        cano, acnt_prdt = self.account_no.split("-")
        tr_id = "VTTC8434R" if settings.is_paper else "TTTC8434R"
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        blank = {
            "deposit": 0.0,
            "stock_eval": 0.0,
            "total_eval": 0.0,
            "purchase": 0.0,
            "pnl": 0.0,
            "pnl_pct": 0.0,
        }
        try:
            resp = self._get_with_retry(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id, params, label="balance",
            )
            data = resp.json()
            out2 = data.get("output2", [])
            if not out2:
                return blank
            r = out2[0]
            deposit = float(r.get("dnca_tot_amt") or 0)
            stock_eval = float(r.get("scts_evlu_amt") or 0)
            total_eval = float(r.get("tot_evlu_amt") or 0)
            purchase = float(r.get("pchs_amt_smtl_amt") or 0)
            pnl = float(r.get("evlu_pfls_smtl_amt") or 0)
            pnl_pct = (pnl / purchase * 100.0) if purchase > 0 else 0.0
            return {
                "deposit": deposit,
                "stock_eval": stock_eval,
                "total_eval": total_eval,
                "purchase": purchase,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        except Exception as exc:
            logger.warning("account summary fetch failed: {}", exc)
        return blank

    def get_positions(self) -> list[dict[str, Any]]:
        """주식 잔고 조회."""
        cano, acnt_prdt = self.account_no.split("-")
        tr_id = "VTTC8434R" if settings.is_paper else "TTTC8434R"
        params = {
            "CANO": cano,
            "ACNT_PRDT_CD": acnt_prdt,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        resp = self._get_with_retry(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id, params, label="positions",
        )
        data = resp.json()
        # rt_cd != "0" 이면 output1 이 아예 없다. 예전엔 .get(..., []) 로 뭉개져
        # '조회 실패'가 '보유 0주'와 구분되지 않았다 — leader_trader._broker_qty 가
        # 이 값을 보고 매도 거부 종목을 '잔고 0'으로 단정해 살아있는 포지션을
        # 종료 처리할 수 있다. 실패는 예외로 올려 호출측이 판단을 보류하게 한다.
        if str(data.get("rt_cd", "0")) != "0":
            raise RuntimeError(
                f"잔고 조회 실패 [{data.get('msg_cd')}] {str(data.get('msg1', '')).strip()}"
            )
        return data.get("output1", [])

    def get_orderbook(self, symbol: str) -> dict[str, Any]:
        """호가창 조회 (매도/매수 각 5단계).

        Returns:
            {
              "asks": [{"price": float, "qty": int}, ...],  # 매도호가 [0]=1위(최우선)
              "bids": [{"price": float, "qty": int}, ...],  # 매수호가 [0]=1위(최우선)
              "total_ask_qty": int,
              "total_bid_qty": int,
            }
        """
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": self._code(symbol)}
        try:
            resp = self._get_with_retry(
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "FHKST01010200", params, label=f"orderbook {symbol}",
            )
        except Exception as exc:
            logger.debug("orderbook 조회 실패 ({}): {}", symbol, exc)
            return {}
        output = resp.json().get("output1", {})
        if not output:
            return {}
        asks: list[dict[str, Any]] = []
        bids: list[dict[str, Any]] = []
        for i in range(1, 6):
            ap = output.get(f"askp{i}", "0") or "0"
            aq = output.get(f"askp_rsqn{i}", "0") or "0"
            bp = output.get(f"bidp{i}", "0") or "0"
            bq = output.get(f"bidp_rsqn{i}", "0") or "0"
            try:
                if float(ap) > 0:
                    asks.append({"price": float(ap), "qty": int(aq)})
            except (ValueError, TypeError):
                pass
            try:
                if float(bp) > 0:
                    bids.append({"price": float(bp), "qty": int(bq)})
            except (ValueError, TypeError):
                pass
        try:
            total_ask = int(output.get("total_askp_rsqn", 0) or 0)
            total_bid = int(output.get("total_bidp_rsqn", 0) or 0)
        except (ValueError, TypeError):
            total_ask = total_bid = 0
        return {
            "asks": asks,
            "bids": bids,
            "total_ask_qty": total_ask,
            "total_bid_qty": total_bid,
        }

    def close(self) -> None:
        """httpx 소켓·게이트 파일 fd 정리. 인스턴스 폐기 전 반드시 호출 —
        _gate_fd 는 raw os fd 라 GC 로도 안 닫혀 방치 시 누수(Errno 24)."""
        try:
            self._client.close()
        except Exception:
            pass
        if self._gate_fd is not None:
            try:
                os.close(self._gate_fd)
            except OSError:
                pass
            self._gate_fd = None
