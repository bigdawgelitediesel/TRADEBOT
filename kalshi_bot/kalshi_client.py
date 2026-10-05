"""Minimal Kalshi Trade API v2 client with RSA-PSS request signing.

Every authenticated request carries:
    KALSHI-ACCESS-KEY        the API key id
    KALSHI-ACCESS-TIMESTAMP  current time in milliseconds
    KALSHI-ACCESS-SIGNATURE  base64( RSA-PSS-SHA256( timestamp + METHOD + path ) )
where `path` is the full request path (including /trade-api/v2) without the query string.
"""
from __future__ import annotations

import base64
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiError(RuntimeError):
    pass


def load_private_key(pem: str) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise KalshiError(
            f"Kalshi requires an RSA private key; got {type(key).__name__}. "
            "Download the key file from Kalshi -> Account -> API Keys."
        )
    return key


def sign(private_key: rsa.RSAPrivateKey, timestamp_ms: str, method: str, path: str) -> str:
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode()


class KalshiClient:
    def __init__(self, base_url: str, api_key_id: str | None = None,
                 private_key_pem: str | None = None, timeout: float = 20.0,
                 session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key = load_private_key(private_key_pem) if private_key_pem else None
        self.timeout = timeout
        self.session = session or requests.Session()

    # ----------------------------------------------------------------- transport
    def _headers(self, method: str, path: str) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.private_key and self.api_key_id:
            ts = str(int(time.time() * 1000))
            headers.update({
                "KALSHI-ACCESS-KEY": self.api_key_id,
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": sign(self.private_key, ts, method, path),
            })
        return headers

    def request(self, method: str, endpoint: str, params: dict | None = None,
                json: dict | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{endpoint}"
        path = urlparse(url).path  # signed path excludes the query string
        resp = self.session.request(method, url, params=params, json=json,
                                    headers=self._headers(method, path), timeout=self.timeout)
        if resp.status_code >= 400:
            raise KalshiError(f"{method} {endpoint} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else {}

    def _require_auth(self) -> None:
        if not (self.private_key and self.api_key_id):
            raise KalshiError("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY are not set")

    # ------------------------------------------------------------------- public
    def exchange_status(self) -> dict:
        return self.request("GET", "/exchange/status")

    def get_markets(self, series_ticker: str | None = None, event_ticker: str | None = None,
                    status: str = "open", limit: int = 200, max_pages: int = 10) -> list[dict]:
        params: dict[str, Any] = {"limit": limit, "status": status}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        markets: list[dict] = []
        for _ in range(max_pages):
            data = self.request("GET", "/markets", params=params)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
            params["cursor"] = cursor
        return markets

    def get_market(self, ticker: str) -> dict:
        return self.request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str, depth: int = 5) -> dict:
        return self.request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # ---------------------------------------------------------------- portfolio
    def get_balance(self) -> dict:
        self._require_auth()
        return self.request("GET", "/portfolio/balance")

    def get_positions(self) -> dict:
        self._require_auth()
        return self.request("GET", "/portfolio/positions", params={"limit": 200})

    def get_orders(self, status: str = "resting") -> list[dict]:
        self._require_auth()
        return self.request("GET", "/portfolio/orders", params={"status": status, "limit": 200}).get("orders", [])

    def place_order(self, ticker: str, side: str, count: int, price_cents: int,
                    action: str = "buy", client_order_id: str | None = None) -> dict:
        """Place a limit order. `side` is 'yes' or 'no'; price is for that side, in cents."""
        self._require_auth()
        if side not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        if not 1 <= price_cents <= 99:
            raise ValueError("price_cents must be 1..99")
        if count < 1:
            raise ValueError("count must be >= 1")
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": "limit",
            "count": int(count),
            "client_order_id": client_order_id or str(uuid.uuid4()),
            f"{side}_price": int(price_cents),
        }
        return self.request("POST", "/portfolio/orders", json=body).get("order", {})

    def cancel_order(self, order_id: str) -> dict:
        self._require_auth()
        return self.request("DELETE", f"/portfolio/orders/{order_id}")
