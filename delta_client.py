"""Delta Exchange client built on the official delta-rest-client SDK.

https://pypi.org/project/delta-rest-client/
"""

from __future__ import annotations

from typing import Any

from delta_rest_client import DeltaRestClient


class DeltaAPIError(Exception):
    def __init__(self, message: str, payload: Any = None):
        super().__init__(message)
        self.payload = payload


class DeltaClient:
    """Thin wrapper around DeltaRestClient for the Mirror Pip copier."""

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip("/")
        self.sdk = DeltaRestClient(
            base_url=self.base_url,
            api_key=api_key,
            api_secret=api_secret,
            raise_for_status=True,
        )

    def _parse(self, response) -> Any:
        data = response.json()
        if data.get("success"):
            return data.get("result")
        raise DeltaAPIError(
            f"Delta API error: {data.get('error') or data}",
            payload=data,
        )

    def test_connection(self) -> dict[str, Any]:
        """Authenticated login check — works across delta-rest-client versions."""
        try:
            # Prefer official helper when available (newer SDK versions)
            if hasattr(self.sdk, "get_all_wallet_balances"):
                balances = self.sdk.get_all_wallet_balances()
            else:
                balances = self._parse(
                    self.sdk.request("GET", "/v2/wallet/balances", auth=True)
                )
            if balances is None:
                balances = []
            if isinstance(balances, dict):
                balances = [balances]
            return {
                "ok": True,
                "logged_in": True,
                "balances_count": len(balances),
            }
        except Exception as exc:  # noqa: BLE001
            raise DeltaAPIError(str(exc)) from exc


    def get_all_positions(self) -> list[dict[str, Any]]:
        """All open margined positions (no product_ids filter)."""
        try:
            response = self.sdk.request("GET", "/v2/positions/margined", auth=True)
            result = self._parse(response)
            if result is None:
                return []
            if isinstance(result, dict):
                return [result]
            return list(result)
        except Exception as exc:  # noqa: BLE001
            raise DeltaAPIError(str(exc)) from exc

    def get_live_orders(self) -> list[dict[str, Any]]:
        try:
            result = self.sdk.get_live_orders()
            if result is None:
                return []
            if isinstance(result, dict):
                return [result]
            return list(result)
        except Exception as exc:  # noqa: BLE001
            raise DeltaAPIError(str(exc)) from exc

    def get_recent_fills(self, page_size: int = 50) -> list[dict[str, Any]]:
        try:
            data = self.sdk.fills(query={}, page_size=page_size)
            if not data.get("success", True) and "error" in data:
                raise DeltaAPIError(f"Delta API error: {data['error']}", payload=data)
            return list(data.get("result") or [])
        except DeltaAPIError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise DeltaAPIError(str(exc)) from exc

    def get_order_by_id(self, order_id: str | int) -> dict[str, Any] | None:
        try:
            result = self.sdk.get_order_by_id(order_id)
            if isinstance(result, dict):
                return result
            return {"result": result}
        except Exception:
            return None
