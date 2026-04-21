"""한국투자증권 KIS OpenAPI 클라이언트.

참고 문서: https://apiportal.koreainvestment.com/apiservice
TR ID 는 모의투자(paper) / 실전(real)에서 다르므로 분기 처리한다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from stock_bot.config import settings

TOKEN_CACHE = Path(".kis_token.json")


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
    def _ensure_token(self) -> str:
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
        logger.info("KIS access token issued")
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
        """일봉 조회 (최근 count 일)."""
        params = {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        }
        resp = self._client.get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            headers=self._headers("FHKST01010400"),
            params=params,
        )
        resp.raise_for_status()
        rows = resp.json().get("output", [])[:count]
        return [
            {
                "date": r["stck_bsop_date"],
                "open": float(r["stck_oprc"]),
                "high": float(r["stck_hgpr"]),
                "low": float(r["stck_lwpr"]),
                "close": float(r["stck_clpr"]),
                "volume": int(r["acml_vol"]),
            }
            for r in rows
        ]

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
        try:
            resp = self._client.get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                headers=self._headers(tr_id),
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            # output2: 잔고 요약. tot_evlu_amt = 총평가금액
            out2 = data.get("output2", [])
            if out2:
                return float(out2[0].get("tot_evlu_amt") or 0)
        except Exception as exc:
            logger.warning("account total fetch failed: {}", exc)
        return 0.0

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
