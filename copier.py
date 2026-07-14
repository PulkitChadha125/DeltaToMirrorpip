"""Background Delta → Mirror Pip position copier.

Polls net positions (default every 300ms, min 200ms) and copies changes:

  open / increase long   → buy   (creates platform_id ↔ mirror_id mapping)
  close / reduce long    → sell  (exits mapped platform position)
  open / increase short  → short (creates platform_id ↔ mirror_id mapping)
  close / reduce short   → cover (exits mapped platform position)
"""

from __future__ import annotations

import csv
import io
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config_store import MIN_POLL_INTERVAL_MS, load_config, load_credentials
from delta_client import DeltaAPIError, DeltaClient
from mirrorpip_client import MirrorPipError, build_payload, extract_mirror_id, send_order

ROOT = Path(__file__).resolve().parent
LOGS_PATH = ROOT / "order_logs.json"
BINDINGS_PATH = ROOT / "order_bindings.json"
POSITIONS_PATH = ROOT / "position_snapshot.json"
PLATFORM_COUNTER_PATH = ROOT / "platform_id_counter.json"
MAX_LOGS = 5000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _to_int_size(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_log_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def map_position_change(prev_size: int, curr_size: int) -> list[tuple[str, int]]:
    actions: list[tuple[str, int]] = []
    if prev_size == curr_size:
        return actions

    if curr_size > prev_size:
        up = curr_size - prev_size
        if prev_size < 0:
            to_flat = -prev_size
            if up <= to_flat:
                actions.append(("cover", up))
            else:
                actions.append(("cover", to_flat))
                actions.append(("buy", up - to_flat))
        else:
            actions.append(("buy", up))
    else:
        down = prev_size - curr_size
        if prev_size > 0:
            to_flat = prev_size
            if down <= to_flat:
                actions.append(("sell", down))
            else:
                actions.append(("sell", to_flat))
                actions.append(("short", down - to_flat))
        else:
            actions.append(("short", down))

    return [(otype, qty) for otype, qty in actions if qty > 0]


class OrderCopier:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        self._status = "stopped"
        self._last_error = ""
        self._positions: dict[str, int] = {
            str(k): _to_int_size(v) for k, v in _read_json(POSITIONS_PATH, {}).items()
        }
        self._position_meta: dict[str, dict[str, Any]] = {}
        # List of mapped positions: delta ↔ platform_id ↔ mirror_id
        raw_bindings = _read_json(BINDINGS_PATH, [])
        if isinstance(raw_bindings, dict):
            # Migrate old dict format
            self._bindings: list[dict[str, Any]] = []
        else:
            self._bindings = list(raw_bindings or [])
        self._logs: list[dict[str, Any]] = _read_json(LOGS_PATH, [])
        self._platform_counter = int(_read_json(PLATFORM_COUNTER_PATH, {"n": 100}).get("n", 100))

    @property
    def running(self) -> bool:
        return self._running

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            open_positions = sum(1 for size in self._positions.values() if size != 0)
            open_maps = sum(1 for b in self._bindings if b.get("status") == "open")
            return {
                "running": self._running,
                "status": self._status,
                "last_error": self._last_error,
                "tracked_products": len(self._positions),
                "open_positions": open_positions,
                "bindings": open_maps,
            }

    def get_logs(
        self,
        *,
        limit: int | None = 200,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._logs)

        symbol_q = (symbol or "").strip().lower()
        from_dt = _parse_log_time(date_from) if date_from else None
        to_dt = _parse_log_time(date_to) if date_to else None
        # If date_to is date-only midnight, treat as end-of-day inclusively by allowing +1 day-ish:
        # UI should send end-of-day ISO; still, if time is 00:00:00 treat as end of that day.
        if to_dt and to_dt.hour == 0 and to_dt.minute == 0 and to_dt.second == 0 and "T" not in (date_to or ""):
            to_dt = to_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

        filtered: list[dict[str, Any]] = []
        for row in rows:
            if symbol_q:
                row_symbol = str(row.get("symbol") or "").lower()
                if symbol_q not in row_symbol:
                    continue
            row_dt = _parse_log_time(str(row.get("time") or ""))
            if from_dt and row_dt and row_dt < from_dt:
                continue
            if to_dt and row_dt and row_dt > to_dt:
                continue
            if from_dt and not row_dt:
                continue
            if to_dt and not row_dt:
                continue
            filtered.append(row)

        if limit is None or limit <= 0:
            return filtered
        return filtered[:limit]

    def get_log(self, log_id: str) -> dict[str, Any] | None:
        with self._lock:
            for row in self._logs:
                if str(row.get("id")) == str(log_id):
                    return dict(row)
        return None

    def get_log_symbols(self) -> list[str]:
        with self._lock:
            symbols = sorted(
                {
                    str(row.get("symbol")).strip()
                    for row in self._logs
                    if row.get("symbol") not in (None, "", "—")
                }
            )
        return symbols

    def logs_to_csv(
        self,
        *,
        symbol: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> str:
        rows = self.get_logs(limit=None, symbol=symbol, date_from=date_from, date_to=date_to)
        output = io.StringIO()
        fieldnames = [
            "time",
            "status",
            "delta_order_id",
            "platform_id",
            "mirror_id",
            "symbol",
            "order_type",
            "quantity",
            "price",
            "tp",
            "sl",
            "prev_size",
            "curr_size",
            "message",
        ]
        writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
        return output.getvalue()

    def clear_logs(self) -> None:
        with self._lock:
            self._logs = []
            _write_json(LOGS_PATH, self._logs)

    def _append_log(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._logs.insert(0, entry)
            self._logs = self._logs[:MAX_LOGS]
            _write_json(LOGS_PATH, self._logs)

    def _save_bindings(self) -> None:
        with self._lock:
            _write_json(BINDINGS_PATH, self._bindings)

    def _save_positions(self) -> None:
        with self._lock:
            _write_json(POSITIONS_PATH, self._positions)

    def _next_platform_id(self) -> str:
        with self._lock:
            self._platform_counter += 1
            _write_json(PLATFORM_COUNTER_PATH, {"n": self._platform_counter})
            return str(self._platform_counter)

    def _set_status(self, status: str, error: str = "") -> None:
        with self._lock:
            self._status = status
            if error:
                self._last_error = error

    def start(self) -> dict[str, Any]:
        if self._running:
            return {"ok": False, "message": "Copier is already running."}

        creds = load_credentials()
        if not creds.get("key") or not creds.get("secret"):
            return {"ok": False, "message": "Missing Delta API key/secret in credentials.csv."}

        cfg = load_config()
        if not cfg.get("code"):
            return {"ok": False, "message": "Mirror Pip code is required."}
        if not cfg.get("mirrorpip_webhook_url"):
            return {"ok": False, "message": "Mirror Pip webhook URL is required."}

        self._status = "logging_in"
        self._last_error = ""

        try:
            client = self._build_client()
            login_response = client.test_connection()
            open_positions = self._bootstrap(client)
            login_response = {
                **login_response,
                "logged_in": True,
                "base_url": cfg.get("delta_base_url") or "https://api.india.delta.exchange",
                "open_positions_snapshotted": open_positions,
                "poll_interval_ms": cfg.get("poll_interval_ms", 300),
                "message": "Delta API login successful",
            }
            print("\n=== Delta API login response ===", flush=True)
            print(json.dumps(login_response, indent=2), flush=True)
            print("=== Copy trading starting ===\n", flush=True)
        except Exception as exc:  # noqa: BLE001
            self._status = "error"
            self._last_error = str(exc)
            self._append_log(
                {
                    "time": _utc_now(),
                    "status": "error",
                    "message": f"Delta login failed: {exc}",
                }
            )
            print(f"\n=== Delta API login FAILED ===\n{exc}\n", flush=True)
            return {
                "ok": False,
                "message": f"Delta login failed: {exc}",
                "login_response": {"logged_in": False, "error": str(exc)},
            }

        self._stop_event.clear()
        self._running = True
        self._status = "running"
        self._thread = threading.Thread(target=self._run_loop, name="delta-copier", daemon=True)
        self._thread.start()

        self._append_log(
            {
                "time": _utc_now(),
                "status": "info",
                "message": "Logged in to Delta API. Copy trading started.",
                "login_response": login_response,
            }
        )

        return {
            "ok": True,
            "message": "Logged in to Delta API successfully. Copy trading started.",
            "login_response": login_response,
        }

    def stop(self) -> dict[str, Any]:
        if not self._running:
            return {"ok": False, "message": "Copier is not running."}
        self._stop_event.set()
        self._status = "stopping"
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=8)
        self._running = False
        self._status = "stopped"
        stop_response = {
            "stopped": True,
            "message": "Copy trading stopped successfully.",
            "time": _utc_now(),
        }
        print("\n=== Copy trading stopped ===", flush=True)
        print(json.dumps(stop_response, indent=2), flush=True)
        print("============================\n", flush=True)
        self._append_log(
            {
                "time": _utc_now(),
                "status": "info",
                "message": "Copy trading stopped.",
            }
        )
        return {
            "ok": True,
            "message": "Copy trading stopped successfully.",
            "stop_response": stop_response,
        }

    def _build_client(self) -> DeltaClient:
        creds = load_credentials()
        cfg = load_config()
        return DeltaClient(
            api_key=creds["key"],
            api_secret=creds["secret"],
            base_url=cfg.get("delta_base_url") or "https://api.india.delta.exchange",
        )

    def _snapshot_positions(self, client: DeltaClient) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for pos in client.get_all_positions():
            pid = str(pos.get("product_id") or "")
            if not pid:
                continue
            size = _to_int_size(pos.get("size"))
            snapshot[pid] = {
                "size": size,
                "symbol": pos.get("product_symbol") or pid,
                "entry_price": str(pos.get("entry_price") or "0"),
                "raw": pos,
            }
        return snapshot

    def _latest_fill_for_product(
        self, fills: list[dict[str, Any]], product_id: str
    ) -> dict[str, Any] | None:
        for fill in fills:
            if str(fill.get("product_id")) == product_id:
                return fill
        return None

    def _extract_tp_sl(
        self,
        product_id: str,
        entry_price: str,
        orders: list[dict[str, Any]],
    ) -> tuple[str, str]:
        try:
            entry = float(entry_price)
        except (TypeError, ValueError):
            entry = 0.0

        tp_abs = None
        sl_abs = None
        for order in orders:
            if str(order.get("product_id")) != product_id:
                continue
            stop_type = (order.get("stop_order_type") or "").lower()
            try:
                level = float(order.get("stop_price") or order.get("limit_price") or 0)
            except (TypeError, ValueError):
                continue
            if level <= 0:
                continue
            if "take_profit" in stop_type and tp_abs is None:
                tp_abs = level
            elif "stop_loss" in stop_type and sl_abs is None:
                sl_abs = level

            for key in ("bracket_take_profit_price", "bracket_take_profit_limit_price"):
                if order.get(key) not in (None, "") and tp_abs is None:
                    try:
                        tp_abs = float(order[key])
                    except (TypeError, ValueError):
                        pass
            for key in ("bracket_stop_loss_price", "bracket_stop_loss_limit_price"):
                if order.get(key) not in (None, "") and sl_abs is None:
                    try:
                        sl_abs = float(order[key])
                    except (TypeError, ValueError):
                        pass

        def dist(level: float | None) -> str:
            if level is None:
                return "0"
            if entry == 0:
                return str(level)
            return str(abs(round(entry - level, 8))).rstrip("0").rstrip(".") or "0"

        return dist(tp_abs), dist(sl_abs)

    def _open_mapped_position(
        self,
        *,
        product_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        tp: str,
        sl: str,
        delta_order_id: str,
        prev_size: int,
        curr_size: int,
        order_type: str,
        delta_response: dict[str, Any] | None = None,
    ) -> None:
        cfg = load_config()
        platform_id = self._next_platform_id()
        provisional_mirror_id = str(uuid.uuid4())
        log_id = str(uuid.uuid4())

        payload = build_payload(
            exchange=cfg.get("exchange") or "delta",
            price=price,
            chart_symbol=symbol,
            order_type=order_type,
            instrument_type=cfg.get("instrument_type") or "NA",
            quantity=str(quantity),
            tp=tp,
            sl=sl,
            code=cfg.get("code") or "",
            platform_id=platform_id,
        )

        mirror_request = {
            "method": "POST",
            "url": cfg["mirrorpip_webhook_url"],
            "headers": {"Content-Type": "application/json", "Accept": "application/json"},
            "body": payload,
        }

        log_base = {
            "id": log_id,
            "time": _utc_now(),
            "delta_order_id": delta_order_id,
            "delta_product_id": product_id,
            "platform_id": platform_id,
            "mirror_id": provisional_mirror_id,
            "symbol": symbol,
            "side": order_type,
            "order_type": order_type,
            "price": price,
            "quantity": str(quantity),
            "tp": tp,
            "sl": sl,
            "prev_size": prev_size,
            "curr_size": curr_size,
            "payload": payload,
            "delta_response": delta_response or {},
            "mirror_request": mirror_request,
            "mirror_response": {},
            "latency_ms": None,
        }

        try:
            result = send_order(cfg["mirrorpip_webhook_url"], payload)
            mirror_id = extract_mirror_id(result, provisional_mirror_id)
            binding = {
                "platform_id": platform_id,
                "delta_product_id": product_id,
                "delta_order_id": delta_order_id,
                "mirror_id": mirror_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "qty_remaining": quantity,
                "status": "open",
                "opened_at": _utc_now(),
                "closed_at": None,
            }
            with self._lock:
                self._bindings.append(binding)
            self._save_bindings()
            self._append_log(
                {
                    **log_base,
                    "mirror_id": mirror_id,
                    "status": "copied",
                    "mirror_request": result.get("mirror_request") or mirror_request,
                    "mirror_response": result.get("mirror_response")
                    or {"status_code": result.get("status_code"), "body": result.get("response")},
                    "latency_ms": result.get("latency_ms"),
                    "message": (
                        f"Opened {side}: Delta {delta_order_id} ↔ platform {platform_id} "
                        f"↔ mirror {mirror_id}; sent '{order_type}' qty {quantity}"
                    ),
                }
            )
            print(
                f"[MAP OPEN] delta={delta_order_id} platform={platform_id} "
                f"mirror={mirror_id} {order_type} qty={quantity} {symbol} "
                f"latency={result.get('latency_ms')}ms",
                flush=True,
            )
        except MirrorPipError as exc:
            self._append_log(
                {
                    **log_base,
                    "status": "error",
                    "mirror_request": {
                        "method": "POST",
                        "url": exc.webhook_url or cfg["mirrorpip_webhook_url"],
                        "body": exc.request_payload or payload,
                    },
                    "mirror_response": {
                        "status_code": exc.status_code,
                        "body": exc.body,
                    },
                    "latency_ms": exc.latency_ms,
                    "message": str(exc),
                }
            )
            self._set_status("running", str(exc))

    def _close_mapped_positions(
        self,
        *,
        product_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        exit_order_type: str,
        delta_order_id: str,
        prev_size: int,
        curr_size: int,
        delta_response: dict[str, Any] | None = None,
    ) -> None:
        """Close FIFO mapped opens for this product/side until quantity is covered."""
        cfg = load_config()
        remaining = quantity
        delta_resp = delta_response or {}

        with self._lock:
            opens = [
                b
                for b in self._bindings
                if b.get("status") == "open"
                and str(b.get("delta_product_id")) == product_id
                and b.get("side") == side
                and int(b.get("qty_remaining") or 0) > 0
            ]

        for binding in opens:
            if remaining <= 0:
                break
            avail = int(binding.get("qty_remaining") or 0)
            close_qty = min(avail, remaining)
            platform_id = str(binding.get("platform_id"))
            mirror_id = str(binding.get("mirror_id"))
            mapped_delta_id = str(binding.get("delta_order_id") or delta_order_id)
            log_id = str(uuid.uuid4())

            payload = build_payload(
                exchange=cfg.get("exchange") or "delta",
                price=price,
                chart_symbol=symbol,
                order_type=exit_order_type,
                instrument_type=cfg.get("instrument_type") or "NA",
                quantity=str(close_qty),
                tp="0",
                sl="0",
                code=cfg.get("code") or "",
                platform_id=platform_id,
                mirror_id=mirror_id,
            )

            mirror_request = {
                "method": "POST",
                "url": cfg["mirrorpip_webhook_url"],
                "headers": {"Content-Type": "application/json", "Accept": "application/json"},
                "body": payload,
            }

            log_base = {
                "id": log_id,
                "time": _utc_now(),
                "delta_order_id": mapped_delta_id,
                "delta_product_id": product_id,
                "platform_id": platform_id,
                "mirror_id": mirror_id,
                "symbol": symbol,
                "side": exit_order_type,
                "order_type": exit_order_type,
                "price": price,
                "quantity": str(close_qty),
                "tp": "0",
                "sl": "0",
                "prev_size": prev_size,
                "curr_size": curr_size,
                "payload": payload,
                "delta_response": delta_resp,
                "mirror_request": mirror_request,
                "mirror_response": {},
                "latency_ms": None,
            }

            try:
                result = send_order(cfg["mirrorpip_webhook_url"], payload)
                new_remaining = avail - close_qty
                with self._lock:
                    for b in self._bindings:
                        if b.get("platform_id") == platform_id:
                            b["qty_remaining"] = new_remaining
                            if new_remaining <= 0:
                                b["status"] = "closed"
                                b["closed_at"] = _utc_now()
                            break
                self._save_bindings()
                self._append_log(
                    {
                        **log_base,
                        "status": "copied",
                        "mirror_request": result.get("mirror_request") or mirror_request,
                        "mirror_response": result.get("mirror_response")
                        or {"status_code": result.get("status_code"), "body": result.get("response")},
                        "latency_ms": result.get("latency_ms"),
                        "message": (
                            f"Exited mapped position platform {platform_id} "
                            f"(mirror {mirror_id}): sent '{exit_order_type}' qty {close_qty}"
                        ),
                    }
                )
                print(
                    f"[MAP CLOSE] delta={mapped_delta_id} platform={platform_id} "
                    f"mirror={mirror_id} {exit_order_type} qty={close_qty} {symbol} "
                    f"latency={result.get('latency_ms')}ms",
                    flush=True,
                )
                remaining -= close_qty
            except MirrorPipError as exc:
                self._append_log(
                    {
                        **log_base,
                        "status": "error",
                        "mirror_request": {
                            "method": "POST",
                            "url": exc.webhook_url or cfg["mirrorpip_webhook_url"],
                            "body": exc.request_payload or payload,
                        },
                        "mirror_response": {
                            "status_code": exc.status_code,
                            "body": exc.body,
                        },
                        "latency_ms": exc.latency_ms,
                        "message": str(exc),
                    }
                )
                self._set_status("running", str(exc))
                break

        if remaining > 0:
            platform_id = self._next_platform_id()
            mirror_id = str(uuid.uuid4())
            log_id = str(uuid.uuid4())
            payload = build_payload(
                exchange=cfg.get("exchange") or "delta",
                price=price,
                chart_symbol=symbol,
                order_type=exit_order_type,
                instrument_type=cfg.get("instrument_type") or "NA",
                quantity=str(remaining),
                tp="0",
                sl="0",
                code=cfg.get("code") or "",
                platform_id=platform_id,
            )
            mirror_request = {
                "method": "POST",
                "url": cfg["mirrorpip_webhook_url"],
                "headers": {"Content-Type": "application/json", "Accept": "application/json"},
                "body": payload,
            }
            log_base = {
                "id": log_id,
                "time": _utc_now(),
                "delta_order_id": delta_order_id,
                "delta_product_id": product_id,
                "platform_id": platform_id,
                "mirror_id": mirror_id,
                "symbol": symbol,
                "side": exit_order_type,
                "order_type": exit_order_type,
                "price": price,
                "quantity": str(remaining),
                "tp": "0",
                "sl": "0",
                "prev_size": prev_size,
                "curr_size": curr_size,
                "payload": payload,
                "delta_response": delta_resp,
                "mirror_request": mirror_request,
                "mirror_response": {},
                "latency_ms": None,
            }
            try:
                result = send_order(cfg["mirrorpip_webhook_url"], payload)
                mirror_id = extract_mirror_id(result, mirror_id)
                self._append_log(
                    {
                        **log_base,
                        "mirror_id": mirror_id,
                        "status": "copied",
                        "mirror_request": result.get("mirror_request") or mirror_request,
                        "mirror_response": result.get("mirror_response")
                        or {"status_code": result.get("status_code"), "body": result.get("response")},
                        "latency_ms": result.get("latency_ms"),
                        "message": (
                            f"Exit without prior map: sent '{exit_order_type}' "
                            f"qty {remaining} (platform {platform_id})"
                        ),
                    }
                )
            except MirrorPipError as exc:
                self._append_log(
                    {
                        **log_base,
                        "status": "error",
                        "mirror_request": {
                            "method": "POST",
                            "url": exc.webhook_url or cfg["mirrorpip_webhook_url"],
                            "body": exc.request_payload or payload,
                        },
                        "mirror_response": {
                            "status_code": exc.status_code,
                            "body": exc.body,
                        },
                        "latency_ms": exc.latency_ms,
                        "message": str(exc),
                    }
                )
                self._set_status("running", str(exc))

    def _process_position_diffs(
        self,
        client: DeltaClient,
        snapshot: dict[str, dict[str, Any]],
    ) -> None:
        fills: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        try:
            fills = client.get_recent_fills(page_size=50)
        except DeltaAPIError:
            fills = []
        try:
            orders = client.get_live_orders()
        except DeltaAPIError:
            orders = []

        product_ids = set(self._positions.keys()) | set(snapshot.keys())

        for pid in sorted(product_ids):
            prev_size = int(self._positions.get(pid, 0))
            meta = snapshot.get(pid) or self._position_meta.get(pid) or {}
            curr_size = int(snapshot[pid]["size"]) if pid in snapshot else 0
            symbol = str(
                (snapshot.get(pid) or {}).get("symbol")
                or meta.get("symbol")
                or pid
            )
            entry_price = str(
                (snapshot.get(pid) or {}).get("entry_price")
                or meta.get("entry_price")
                or "0"
            )
            fill = self._latest_fill_for_product(fills, pid)
            price = str((fill or {}).get("price") or entry_price or "0")
            delta_order_id = str((fill or {}).get("order_id") or (fill or {}).get("id") or f"{pid}:{_utc_now()}")

            order_detail = None
            if fill and fill.get("order_id") not in (None, ""):
                order_detail = client.get_order_by_id(fill.get("order_id"))

            delta_response = {
                "product_id": pid,
                "symbol": symbol,
                "prev_size": prev_size,
                "curr_size": curr_size,
                "position": (snapshot.get(pid) or {}).get("raw")
                or {
                    "product_id": pid,
                    "product_symbol": symbol,
                    "size": curr_size,
                    "entry_price": entry_price,
                },
                "fill": fill,
                "order": order_detail,
            }

            actions = map_position_change(prev_size, curr_size)
            for order_type, qty in actions:
                if order_type == "buy":
                    tp, sl = self._extract_tp_sl(pid, price, orders)
                    self._open_mapped_position(
                        product_id=pid,
                        symbol=symbol,
                        side="long",
                        quantity=qty,
                        price=price,
                        tp=tp,
                        sl=sl,
                        delta_order_id=delta_order_id,
                        prev_size=prev_size,
                        curr_size=curr_size,
                        order_type="buy",
                        delta_response=delta_response,
                    )
                elif order_type == "short":
                    tp, sl = self._extract_tp_sl(pid, price, orders)
                    self._open_mapped_position(
                        product_id=pid,
                        symbol=symbol,
                        side="short",
                        quantity=qty,
                        price=price,
                        tp=tp,
                        sl=sl,
                        delta_order_id=delta_order_id,
                        prev_size=prev_size,
                        curr_size=curr_size,
                        order_type="short",
                        delta_response=delta_response,
                    )
                elif order_type == "sell":
                    self._close_mapped_positions(
                        product_id=pid,
                        symbol=symbol,
                        side="long",
                        quantity=qty,
                        price=price,
                        exit_order_type="sell",
                        delta_order_id=delta_order_id,
                        prev_size=prev_size,
                        curr_size=curr_size,
                        delta_response=delta_response,
                    )
                elif order_type == "cover":
                    self._close_mapped_positions(
                        product_id=pid,
                        symbol=symbol,
                        side="short",
                        quantity=qty,
                        price=price,
                        exit_order_type="cover",
                        delta_order_id=delta_order_id,
                        prev_size=prev_size,
                        curr_size=curr_size,
                        delta_response=delta_response,
                    )

            if curr_size == 0:
                self._positions.pop(pid, None)
                self._position_meta.pop(pid, None)
            else:
                self._positions[pid] = curr_size
                self._position_meta[pid] = {
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "size": curr_size,
                }

        self._save_positions()

    def _bootstrap(self, client: DeltaClient) -> int:
        snapshot = self._snapshot_positions(client)
        self._positions = {}
        self._position_meta = {}
        for pid, meta in snapshot.items():
            size = int(meta["size"])
            if size == 0:
                continue
            self._positions[pid] = size
            self._position_meta[pid] = {
                "symbol": meta["symbol"],
                "entry_price": meta["entry_price"],
                "size": size,
            }
        self._save_positions()
        count = len(self._positions)
        self._append_log(
            {
                "time": _utc_now(),
                "status": "info",
                "message": (
                    f"Watching net positions every "
                    f"{load_config().get('poll_interval_ms', 300)}ms. "
                    f"Snapshotted {count} open position(s) (not copied)."
                ),
            }
        )
        return count

    def _poll_wait_seconds(self) -> float:
        cfg = load_config()
        ms = int(cfg.get("poll_interval_ms") or 300)
        ms = max(MIN_POLL_INTERVAL_MS, ms)
        return ms / 1000.0

    def _run_loop(self) -> None:
        client = self._build_client()
        while not self._stop_event.is_set():
            try:
                # Refresh credentials occasionally by rebuilding client lightly
                client = self._build_client()
                snapshot = self._snapshot_positions(client)
                self._process_position_diffs(client, snapshot)
                self._stop_event.wait(self._poll_wait_seconds())
            except DeltaAPIError as exc:
                self._set_status("running", str(exc))
                self._append_log(
                    {
                        "time": _utc_now(),
                        "status": "error",
                        "message": f"Delta poll error: {exc}",
                    }
                )
                self._stop_event.wait(1)
            except Exception as exc:  # noqa: BLE001
                self._set_status("running", str(exc))
                self._append_log(
                    {
                        "time": _utc_now(),
                        "status": "error",
                        "message": f"Copier error: {exc}",
                    }
                )
                self._stop_event.wait(1)

        self._running = False
        self._set_status("stopped")


copier = OrderCopier()
