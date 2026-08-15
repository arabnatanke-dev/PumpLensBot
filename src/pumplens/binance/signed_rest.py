"""Read-only signed Binance client; no trading methods exist. / Read-only Binance клиент."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from pumplens.binance.permissions import PermissionDecision, validate_read_only_permissions


class BinanceCredentialError(ValueError):
    """Safe connection error without keys or payloads. / Безопасная ошибка подключения."""


@dataclass(frozen=True, slots=True)
class CredentialVerification:
    permission: PermissionDecision
    permissions: dict[str, bool]
    spot_readable: bool
    futures_readable: bool
    position_risk_readable: bool


class BinanceReadOnlyClient:
    """Expose only permission/account reads. / Предоставляет только чтение прав и аккаунта."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        spot_base_url: str = "https://api.binance.com",
        futures_base_url: str = "https://fapi.binance.com",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key.encode()
        headers = {"X-MBX-APIKEY": api_key}
        timeout = httpx.Timeout(timeout_seconds)
        self._spot = httpx.AsyncClient(
            base_url=spot_base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )
        self._futures = httpx.AsyncClient(
            base_url=futures_base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    async def __aenter__(self) -> BinanceReadOnlyClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._spot.aclose()
        await self._futures.aclose()

    async def verify_read_only(self) -> CredentialVerification:
        """Fail closed before any credential is persisted. / Проверяет ключ до сохранения."""

        permissions = await self.api_restrictions()
        decision = validate_read_only_permissions(permissions)
        if not decision.accepted:
            raise BinanceCredentialError(decision.reason)

        await self.spot_account()
        await self.futures_account()
        await self.position_risk()
        return CredentialVerification(
            permission=decision,
            permissions={key: bool(value) for key, value in permissions.items()},
            spot_readable=True,
            futures_readable=True,
            position_risk_readable=True,
        )

    async def api_restrictions(self) -> Mapping[str, Any]:
        payload = await self._signed_get(self._spot, "/sapi/v1/account/apiRestrictions")
        if not isinstance(payload, Mapping):
            raise BinanceCredentialError("permissions_response_invalid")
        return payload

    async def spot_account(self) -> Mapping[str, Any]:
        payload = await self._signed_get(
            self._spot,
            "/api/v3/account",
            {"omitZeroBalances": "true"},
        )
        if not isinstance(payload, Mapping):
            raise BinanceCredentialError("spot_account_response_invalid")
        return payload

    async def futures_account(self) -> Mapping[str, Any]:
        payload = await self._signed_get(self._futures, "/fapi/v3/account")
        if not isinstance(payload, Mapping):
            raise BinanceCredentialError("futures_account_response_invalid")
        return payload

    async def position_risk(self) -> list[Mapping[str, Any]]:
        payload = await self._signed_get(self._futures, "/fapi/v3/positionRisk")
        if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
            raise BinanceCredentialError("position_risk_response_invalid")
        return payload

    async def spot_prices(self) -> dict[str, float]:
        try:
            response = await self._spot.get("/api/v3/ticker/price")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceCredentialError("spot_prices_unavailable") from exc
        if not isinstance(payload, list):
            raise BinanceCredentialError("spot_prices_response_invalid")
        return {
            str(item["symbol"]): float(item["price"])
            for item in payload
            if isinstance(item, Mapping) and "symbol" in item and "price" in item
        }

    async def start_futures_user_stream(self) -> str:
        """Create a USD-M listen key. / Создаёт listen key USD-M."""

        payload = await self._futures_user_stream_request("POST")
        listen_key = payload.get("listenKey") if isinstance(payload, Mapping) else None
        if not isinstance(listen_key, str) or not listen_key:
            raise BinanceCredentialError("futures_listen_key_response_invalid")
        return listen_key

    async def keepalive_futures_user_stream(self) -> None:
        """Refresh the current USD-M listen key. / Продлевает listen key USD-M."""

        await self._futures_user_stream_request("PUT")

    async def close_futures_user_stream(self) -> None:
        """Close the current USD-M listen key. / Закрывает listen key USD-M."""

        await self._futures_user_stream_request("DELETE")

    def signed_websocket_params(self) -> dict[str, str | int]:
        """Build HMAC params for Spot WS API. / Подписывает параметры Spot WS API."""

        params: dict[str, str | int] = {
            "apiKey": self._api_key,
            "timestamp": int(time.time() * 1_000),
        }
        params["signature"] = self._signature(params)
        return params

    async def _futures_user_stream_request(self, method: str) -> Any:
        try:
            response = await self._futures.request(method, "/fapi/v1/listenKey")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceCredentialError("futures_user_stream_unavailable") from exc

    async def _signed_get(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Mapping[str, str | int] | None = None,
    ) -> Any:
        query: dict[str, str | int] = dict(params or {})
        query["timestamp"] = int(time.time() * 1_000)
        query["recvWindow"] = 5_000
        query = dict(sorted(query.items()))
        query["signature"] = self._signature(query)
        try:
            response = await client.get(path, params=query)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Do not leak response bodies: exchanges may echo request details.
            # Не выводим response body: биржа может вернуть детали запроса.
            raise BinanceCredentialError("binance_read_verification_failed") from exc

    def _signature(self, params: Mapping[str, str | int]) -> str:
        # Binance signs parameters in alphabetical order for WebSocket requests;
        # sorting also keeps REST signatures deterministic. / Для WS Binance требует
        # алфавитный порядок; сортировка делает и REST-подпись детерминированной.
        encoded = urlencode(sorted(params.items()))
        return hmac.new(self._secret_key, encoded.encode(), hashlib.sha256).hexdigest()
