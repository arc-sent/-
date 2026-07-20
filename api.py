"""Асинхронный клиент API prmotion.me.

API-ключ передаётся в каждый вызов (у каждого пользователя свой, хранится в БД).
Ошибки API возвращаются в JSON в поле ``error`` — для таких случаев поднимается
``ApiError``.
"""
from __future__ import annotations

import logging
import ssl
from typing import Any

import aiohttp

import config

logger = logging.getLogger("prmotion-bot")


def _build_ssl_context() -> ssl.SSLContext:
    """SSL-контекст для запросов к API.

    Порядок доверия:
    1. VERIFY_SSL=false — проверка выключена (крайний случай);
    2. свой CA_BUNDLE, если задан;
    3. системное хранилище Windows/ОС через ``truststore`` — видит и настоящие CA,
       и корни перехватывающих антивирусов/прокси;
    4. запасной вариант — бандл ``certifi``.
    """
    if not config.VERIFY_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logger.warning("Проверка SSL-сертификата отключена (VERIFY_SSL=false).")
        return ctx
    if config.CA_BUNDLE:
        return ssl.create_default_context(cafile=config.CA_BUNDLE)
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())


class ApiError(Exception):
    """Ошибка, возвращённая API в поле ``error`` (или проблема сети/парсинга)."""


class PrmotionAPI:
    def __init__(self, api_url: str = config.API_URL):
        self._url = api_url
        self._session: aiohttp.ClientSession | None = None
        self._ssl: ssl.SSLContext | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._ssl is None:
                self._ssl = _build_ssl_context()
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector(ssl=self._ssl)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, api_key: str, params: dict[str, Any]) -> Any:
        payload = {"key": api_key, **params}
        session = await self._get_session()
        try:
            if method == "POST":
                resp = await session.post(self._url, data=payload)
            else:
                resp = await session.get(self._url, params=payload)
            async with resp:
                text = await resp.text()
                try:
                    data = await resp.json(content_type=None)
                except Exception as exc:  # ответ не JSON
                    raise ApiError(
                        f"Некорректный ответ API (HTTP {resp.status}): {text[:200]}"
                    ) from exc
        except aiohttp.ClientError as exc:
            raise ApiError(f"Сетевая ошибка: {exc}") from exc

        if isinstance(data, dict) and data.get("error"):
            raise ApiError(str(data["error"]))
        return data

    # --- Публичные методы ---

    async def services(self, api_key: str) -> list[dict[str, Any]]:
        return await self._request("GET", api_key, {"action": "services"})

    async def balance(self, api_key: str, currency: str = config.DEFAULT_CURRENCY) -> dict[str, Any]:
        return await self._request("GET", api_key, {"action": "balance", "currency": currency})

    async def status(self, api_key: str, order: str | int) -> dict[str, Any]:
        return await self._request("GET", api_key, {"action": "status", "order": order})

    async def multi_status(self, api_key: str, orders: list[str | int]) -> dict[str, Any]:
        joined = ",".join(str(o) for o in orders)
        return await self._request("GET", api_key, {"action": "status", "orders": joined})

    async def add_order(self, api_key: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", api_key, {"action": "add", **params})


api = PrmotionAPI()
