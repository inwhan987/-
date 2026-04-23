"""한국투자증권 KIS OpenAPI 클라이언트.

참고 문서: https://apiportal.koreainvestment.com/apiservice
TR ID 는 모의투자(paper) / 실전(real)에서 다르므로 분기 처리한다.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from stock_bot.config import settings

# 환경별로 토큰이 다르므로 paper/real 분리 캐시
TOKEN_CACHE_DIR = Path(".kis_tokens")


@dataclass
class Quote:
    symbol: str
    price: float
    change_pct: float


class KISBroker:
    def __init__(self) -> None:
        self.base_url = settings.kis_base_url
        self.app_key = settings.kis_app_key
        self.app_secret = settings.kis_app_secret
        self.account_no = settings.kis_account_no
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._client = httpx.Client(base_url=self.base_url, timeout=10.0)

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

    # ---------- Market data ----------
    def get_quote(self, symbol: str) -> Quote:
        """현재가 조회 (국내주식 현재가)."""
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol}
        resp = self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._headers("FHKST01010100"),
            params=params,
        )
        resp.raise_for_status()
        output = resp.json()["output"]
        return Quote(
            symbol=symbol,
            price=float(output["stck_prpr"]),
            change_pct=float(output["prdy_ctrt"]),
        )

    def get_minute_ohlcv(
        self, symbol: str, interval_min: int = 5, count: int = 120
    ) -> list[dict[str, Any]]:
        """당일 분봉 조회 (국내주식 당일분봉).

        interval_min: 1/5/10/30/60 분봉. KIS 는 `FID_INPUT_HOUR_1` 기준 역순 반환.
        """
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": f"{interval_min:02d}0000",
            "FID_PW_DATA_INCU_YN": "N",
        }
        resp = self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            headers=self._headers("FHKST03010200"),
            params=params,
        )
        resp.raise_for_status()
        rows = resp.json().get("output2", [])[:count]
        return [
            {
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
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.get(
                    "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                    headers=self._headers("FHKST03010100"),
                    params=params,
                )
                resp.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code < 500 or attempt == 2:
                    raise
                wait = 1.5 * (2 ** attempt)
                logger.warning(
                    "KIS daily OHLCV {} returned {}, retry {}/2 after {:.1f}s",
                    symbol, exc.response.status_code, attempt + 1, wait,
                )
                time.sleep(wait)
        else:
            if last_exc:
                raise last_exc
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
            "PDNO": symbol,
            "ORD_DVSN": "01" if order_type == "market" else "00",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0" if order_type == "market" else str(int(price)),
        }
        resp = self._client.post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(self._order_tr_id(side)),
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        logger.info("order: {} {} {} -> {}", side, symbol, quantity, data.get("msg1"))
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
            resp = self._client.get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                headers=self._headers(tr_id),
                params=params,
            )
            resp.raise_for_status()
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
        resp = self._client.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            headers=self._headers(tr_id),
            params=params,
        )
        resp.raise_for_status()
        return resp.json().get("output1", [])

    def close(self) -> None:
        self._client.close()
