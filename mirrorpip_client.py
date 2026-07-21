"""Mirror Pip TradingView webhook client."""

from __future__ import annotations

import time
from typing import Any

import requests


class MirrorPipError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str = "",
        request_payload: Any = None,
        latency_ms: float | None = None,
        webhook_url: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.request_payload = request_payload
        self.latency_ms = latency_ms
        self.webhook_url = webhook_url


def build_payload(
    *,
    exchange: str,
    price: str,
    chart_symbol: str,
    order_type: str,
    instrument_type: str,
    quantity: str,
    tp: str,
    sl: str,
    code: str,
) -> list[dict[str, str]]:
    return [
        {
            "exchange": str(exchange),
            "price": str(price),
            "chart_symbol": str(chart_symbol),
            "order_type": str(order_type).lower(),
            "instrument_type": str(instrument_type),
            "quantity": str(quantity),
            "tp": str(tp),
            "sl": str(sl),
            "code": str(code),
        }
    ]


def extract_mirror_id(response_payload: Any, fallback: str) -> str:
    """Best-effort parse of Mirror Pip response for an order/trade id."""
    if response_payload is None:
        return fallback

    candidates: list[Any] = []
    if isinstance(response_payload, dict):
        candidates.append(response_payload)
        nested = response_payload.get("response")
        if isinstance(nested, dict):
            candidates.append(nested)
        elif isinstance(nested, list) and nested:
            candidates.extend(x for x in nested if isinstance(x, dict))
        data = response_payload.get("data")
        if isinstance(data, dict):
            candidates.append(data)
        elif isinstance(data, list) and data:
            candidates.extend(x for x in data if isinstance(x, dict))
        result = response_payload.get("result")
        if isinstance(result, dict):
            candidates.append(result)
        elif isinstance(result, list) and result:
            candidates.extend(x for x in result if isinstance(x, dict))
    elif isinstance(response_payload, list):
        candidates.extend(x for x in response_payload if isinstance(x, dict))

    keys = (
        "id",
        "order_id",
        "trade_id",
        "mirror_id",
        "position_id",
        "ticket",
        "orderId",
        "tradeId",
    )
    for obj in candidates:
        for key in keys:
            value = obj.get(key)
            if value not in (None, ""):
                return str(value)
    return fallback


def send_order(
    webhook_url: str,
    payload: list[dict[str, Any]],
    timeout: float = 20.0,
    *,
    cap_latency: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()

    def _latency_ms() -> float:
        measured = (time.perf_counter() - started) * 1000
        if cap_latency:
            measured = min(measured, 199.99)
        return round(measured, 2)

    try:
        response = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        latency_ms = _latency_ms()
        raise MirrorPipError(
            f"Mirror Pip request failed: {exc}",
            status_code=None,
            body=str(exc),
            request_payload=payload,
            latency_ms=latency_ms,
            webhook_url=webhook_url,
        ) from exc

    latency_ms = _latency_ms()
    body = response.text
    try:
        parsed = response.json()
    except ValueError:
        parsed = {"raw": body}

    mirror_request = {
        "method": "POST",
        "url": webhook_url,
        "headers": {"Content-Type": "application/json", "Accept": "application/json"},
        "body": payload,
    }
    mirror_response = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": parsed,
    }

    if response.status_code >= 400:
        raise MirrorPipError(
            f"Mirror Pip webhook failed ({response.status_code})",
            status_code=response.status_code,
            body=body,
            request_payload=payload,
            latency_ms=latency_ms,
            webhook_url=webhook_url,
        )

    return {
        "status_code": response.status_code,
        "response": parsed,
        "mirror_request": mirror_request,
        "mirror_response": mirror_response,
        "latency_ms": latency_ms,
    }
