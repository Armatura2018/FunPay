from __future__ import annotations
from typing import Any
import httpx

class VexboostError(RuntimeError):
    pass

class VexboostClient:
    """Клиент Vexboost API по параметрам из предоставленных скриншотов."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _request(self, action: str, **params: Any) -> Any:
        if not self.api_key:
            raise VexboostError("Не задан API-ключ Vexboost")
        try:
            response = self.client.get(
                self.base_url,
                params={"action": action, **params, "key": self.api_key},
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise VexboostError(f"Ошибка запроса Vexboost ({action}): {exc}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise VexboostError(f"Vexboost вернул ошибку: {data['error']}")
        return data

    def services(self) -> Any:
        return self._request("services")

    def balance(self) -> Any:
        return self._request("balance")

    def create_order(self, service_id: int, link: str, quantity: int) -> int:
        if service_id <= 0 or quantity <= 0:
            raise ValueError("service_id и quantity должны быть положительными")
        data = self._request("add", service=service_id, link=link, quantity=quantity)
        if not isinstance(data, dict) or "order" not in data:
            raise VexboostError(f"Неожиданный ответ создания заказа: {data!r}")
        return int(data["order"])

    def order_status(self, order_id: int) -> dict:
        data = self._request("status", order=order_id)
        if not isinstance(data, dict):
            raise VexboostError(f"Неожиданный ответ статуса: {data!r}")
        return data

    def multiple_status(self, order_ids: list[int]) -> dict:
        return {} if not order_ids else self._request("status", orders=",".join(map(str, order_ids)))

    def cancel_order(self, order_id: int) -> bool:
        data = self._request("cancel", order=order_id)
        if not isinstance(data, dict) or "ok" not in data:
            raise VexboostError(f"Неожиданный ответ отмены: {data!r}")
        return str(data["ok"]).lower() == "true"
