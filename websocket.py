"""Delta Exchange private WebSocket client for real-time position updates.

Flow (positions channel, all symbols):
  1. Open WebSocket connection
  2. Send key-auth immediately on open
  3. Enable server heartbeat
  4. Wait for auth success
  5. Subscribe to positions with symbols: ["all"]
  6. Receive snapshot (current open positions)
  7. Receive incremental updates (create / update / delete)

Auth signature:
  HMAC_SHA256(api_secret, "GET" + timestamp + "/live")
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import json
import logging
import queue
import site
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PRODUCTION_WS_URL = "wss://socket.india.delta.exchange"
HEARTBEAT_INTERVAL_SEC = 30
HEARTBEAT_TIMEOUT_SEC = 35
HEARTBEAT_CHECK_INTERVAL_SEC = 5
_WS_VENDOR_PKG = "_delta_ws_client_pkg"
_WebSocketApp = None


def _load_websocket_app():
    """Load WebSocketApp from websocket-client without conflicting with this module.

    This project file is named websocket.py, so a normal ``import websocket``
    would resolve here. Load the installed package from site-packages under an
    alias so its relative imports (from ._logging, etc.) still work.
    """
    global _WebSocketApp
    if _WebSocketApp is not None:
        return _WebSocketApp

    if _WS_VENDOR_PKG in sys.modules:
        _WebSocketApp = sys.modules[_WS_VENDOR_PKG].WebSocketApp
        return _WebSocketApp

    search_roots: list[str] = []
    try:
        search_roots.extend(site.getsitepackages())
    except Exception:  # noqa: BLE001
        pass
    try:
        user_site = site.getusersitepackages()
        if user_site:
            search_roots.append(user_site)
    except Exception:  # noqa: BLE001
        pass
    # Also cover venv site-packages next to this project
    venv_site = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
    if venv_site.is_dir():
        search_roots.append(str(venv_site))

    for base in search_roots:
        if not base:
            continue
        pkg_dir = Path(base) / "websocket"
        init_path = pkg_dir / "__init__.py"
        if not init_path.is_file():
            continue
        # Skip if this somehow points at our project module
        if Path(base).resolve() == Path(__file__).resolve().parent:
            continue

        spec = importlib.util.spec_from_file_location(
            _WS_VENDOR_PKG,
            init_path,
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_WS_VENDOR_PKG] = mod
        spec.loader.exec_module(mod)
        _WebSocketApp = mod.WebSocketApp
        return _WebSocketApp

    raise ImportError(
        "websocket-client is required for WebSocket mode. "
        "Install with: pip install websocket-client"
    )


def rest_base_to_ws_url(base_url: str) -> str:
    """Map Delta REST base URL to the matching private WebSocket endpoint."""
    url = (base_url or "").rstrip("/").lower()
    if "testnet" in url or "deltaex.org" in url:
        if "india" in url or "cdn-ind" in url or "ind" in url:
            return "wss://socket-ind.testnet.deltaex.org"
        return "wss://socket.testnet.deltaex.org"
    if "india" in url:
        return PRODUCTION_WS_URL
    return "wss://socket.delta.exchange"


def _to_int_size(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _position_to_snapshot_entry(pos: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    pid = str(pos.get("product_id") or "")
    if not pid:
        return None
    size = _to_int_size(pos.get("size"))
    symbol = str(pos.get("product_symbol") or pos.get("symbol") or pid)
    entry_price = str(pos.get("entry_price") or "0")
    return pid, {
        "size": size,
        "symbol": symbol,
        "entry_price": entry_price,
        "raw": pos,
    }


class DeltaPositionWebSocket:
    """Authenticated Delta positions WebSocket with heartbeat and reconnect."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        ws_url: str,
        stop_event: threading.Event,
        on_positions_changed: Callable[[], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.ws_url = ws_url or PRODUCTION_WS_URL
        self.stop_event = stop_event
        self.on_positions_changed = on_positions_changed
        self.client_id = str(uuid.uuid4())

        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._ws_app: Any = None
        self._ws_handle: Any = None
        self._connected = False
        self._authenticated = False
        self._subscribed = False
        self._last_error = ""
        self._reconnect_delay = 1.0
        self._last_heartbeat_at = 0.0

    @property
    def connected(self) -> bool:
        return self._connected and self._authenticated and self._subscribed

    @property
    def last_error(self) -> str:
        return self._last_error

    def seed_cache(self, snapshot: dict[str, dict[str, Any]]) -> None:
        """Seed cache from REST bootstrap snapshot before WS snapshot arrives."""
        with self._lock:
            self._cache = {
                pid: {
                    "size": int(meta.get("size", 0)),
                    "symbol": str(meta.get("symbol") or pid),
                    "entry_price": str(meta.get("entry_price") or "0"),
                    "raw": dict(meta.get("raw") or {}),
                }
                for pid, meta in snapshot.items()
            }

    def get_snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                pid: {
                    "size": int(meta["size"]),
                    "symbol": str(meta["symbol"]),
                    "entry_price": str(meta["entry_price"]),
                    "raw": dict(meta.get("raw") or {}),
                }
                for pid, meta in self._cache.items()
            }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_forever,
            name=f"delta-ws-{self.client_id[:8]}",
            daemon=True,
        )
        self._thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._heartbeat_watchdog,
            name=f"delta-ws-hb-{self.client_id[:8]}",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self) -> None:
        ws = self._ws_handle or self._ws_app
        if ws is not None and self._authenticated:
            try:
                self._unsubscribe_positions(ws)
                self._disable_heartbeat(ws)
            except Exception:  # noqa: BLE001
                pass
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _generate_signature(self, timestamp: str) -> str:
        message = f"GET{timestamp}/live"
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return digest.hexdigest()

    def _send_json(self, ws: Any, payload: dict[str, Any]) -> None:
        ws.send(json.dumps(payload))

    def _send_auth(self, ws: Any) -> None:
        timestamp = str(int(time.time()))
        self._send_json(
            ws,
            {
                "type": "key-auth",
                "payload": {
                    "api-key": self.api_key,
                    "signature": self._generate_signature(timestamp),
                    "timestamp": timestamp,
                },
            },
        )

    def _enable_heartbeat(self, ws: Any) -> None:
        self._send_json(ws, {"type": "enable_heartbeat"})
        self._last_heartbeat_at = time.time()

    def _disable_heartbeat(self, ws: Any) -> None:
        self._send_json(ws, {"type": "disable_heartbeat"})

    def _subscribe_positions(self, ws: Any) -> None:
        self._send_json(
            ws,
            {
                "type": "subscribe",
                "payload": {
                    "channels": [
                        {
                            "name": "positions",
                            "symbols": ["all"],
                        }
                    ]
                },
            },
        )
        self._subscribed = True

    def _unsubscribe_positions(self, ws: Any) -> None:
        self._send_json(
            ws,
            {
                "type": "unsubscribe",
                "payload": {
                    "channels": [
                        {
                            "name": "positions",
                        }
                    ]
                },
            },
        )
        self._subscribed = False

    def _apply_positions_message(self, message: dict[str, Any]) -> bool:
        """Apply snapshot or incremental update. Returns True if copy logic should run."""
        action = (message.get("action") or "").lower()
        notify = action in {"create", "update", "delete"}
        changed = False

        with self._lock:
            if action == "snapshot":
                if message.get("success") is False:
                    return False
                result = message.get("result") or []
                if not isinstance(result, list):
                    result = [result]
                self._cache.clear()
                for pos in result:
                    if not isinstance(pos, dict):
                        continue
                    entry = _position_to_snapshot_entry(pos)
                    if not entry:
                        continue
                    pid, meta = entry
                    if meta["size"] == 0:
                        continue
                    self._cache[pid] = meta
                return False

            entry = _position_to_snapshot_entry(message)
            if not entry:
                return False
            pid, meta = entry

            if meta["size"] == 0 or action == "delete":
                if pid in self._cache:
                    self._cache.pop(pid, None)
                    changed = True
            else:
                prev = self._cache.get(pid)
                if (
                    not prev
                    or prev.get("size") != meta["size"]
                    or prev.get("entry_price") != meta["entry_price"]
                    or prev.get("symbol") != meta["symbol"]
                ):
                    self._cache[pid] = meta
                    changed = True

        return notify and changed

    def _notify_change(self) -> None:
        if self.on_positions_changed:
            try:
                self.on_positions_changed()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Position change callback failed: %s", exc)

    def _on_open(self, ws: Any) -> None:
        self._ws_handle = ws
        self._connected = True
        self._authenticated = False
        self._subscribed = False
        self._reconnect_delay = 1.0
        self._send_auth(ws)
        self._enable_heartbeat(ws)

    def _on_message(self, ws: Any, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = message.get("type")

        if msg_type == "heartbeat":
            self._last_heartbeat_at = time.time()
            return

        if msg_type == "key-auth":
            if message.get("success"):
                self._authenticated = True
                self._last_error = ""
                self._subscribe_positions(ws)
                logger.info("Delta WS authenticated [%s]", self.client_id)
            else:
                self._authenticated = False
                self._last_error = message.get("message") or str(message)
                logger.error("Delta WS auth failed [%s]: %s", self.client_id, self._last_error)
            return

        if msg_type == "positions" and self._apply_positions_message(message):
            self._notify_change()

    def _on_error(self, ws: Any, error: Any) -> None:
        self._last_error = str(error)
        logger.warning("Delta WS error [%s]: %s", self.client_id, error)

    def _on_close(self, ws: Any, status_code: Any, close_msg: Any) -> None:
        self._connected = False
        self._authenticated = False
        self._subscribed = False
        self._ws_handle = None
        logger.info(
            "Delta WS closed [%s] code=%s msg=%s",
            self.client_id,
            status_code,
            close_msg,
        )

    def _heartbeat_watchdog(self) -> None:
        while not self.stop_event.is_set():
            if (
                self._connected
                and self._authenticated
                and self._last_heartbeat_at > 0
                and (time.time() - self._last_heartbeat_at) > HEARTBEAT_TIMEOUT_SEC
            ):
                logger.warning(
                    "Delta WS heartbeat timeout [%s]; reconnecting",
                    self.client_id,
                )
                self._last_error = "Heartbeat timeout — reconnecting"
                ws = self._ws_handle or self._ws_app
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:  # noqa: BLE001
                        pass
            self.stop_event.wait(HEARTBEAT_CHECK_INTERVAL_SEC)

    def _run_forever(self) -> None:
        WebSocketApp = _load_websocket_app()
        while not self.stop_event.is_set():
            try:
                self._ws_app = WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws_app.run_forever()
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                logger.exception("Delta WS run error [%s]: %s", self.client_id, exc)
            finally:
                self._connected = False
                self._authenticated = False
                self._subscribed = False
                self._ws_handle = None
                self._ws_app = None

            if self.stop_event.is_set():
                break

            self.stop_event.wait(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)


class PositionUpdateQueue:
    """Thread-safe queue coalescing rapid position updates."""

    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._signal = "update"

    def notify(self) -> None:
        if self._queue.empty():
            self._queue.put(self._signal)

    def wait(self, timeout: float) -> bool:
        try:
            self._queue.get(timeout=timeout)
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            return True
        except queue.Empty:
            return False
