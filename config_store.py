"""Load and persist app config + Delta credentials."""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
CREDENTIALS_PATH = ROOT / "credentials.csv"

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "exchange": "delta",
    "code": "",
    "mirrorpip_webhook_url": "https://trade.mirrorpip.com/tradingview",
    "delta_base_url": "https://api.india.delta.exchange",
    "poll_interval_ms": 300,
    "instrument_type": "NA",
}

MIN_POLL_INTERVAL_MS = 200


def _normalize_config(data: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULT_CONFIG)
    merged.update(data or {})

    # Migrate legacy seconds → milliseconds
    if "poll_interval_ms" not in data and "poll_interval_seconds" in data:
        try:
            merged["poll_interval_ms"] = max(
                MIN_POLL_INTERVAL_MS,
                int(float(data["poll_interval_seconds"]) * 1000),
            )
        except (TypeError, ValueError):
            merged["poll_interval_ms"] = DEFAULT_CONFIG["poll_interval_ms"]

    try:
        merged["poll_interval_ms"] = max(
            MIN_POLL_INTERVAL_MS,
            int(float(merged.get("poll_interval_ms", DEFAULT_CONFIG["poll_interval_ms"]))),
        )
    except (TypeError, ValueError):
        merged["poll_interval_ms"] = DEFAULT_CONFIG["poll_interval_ms"]

    merged.pop("poll_interval_seconds", None)
    return merged


def load_config() -> dict[str, Any]:
    with _lock:
        if not CONFIG_PATH.exists():
            data = dict(DEFAULT_CONFIG)
            CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return _normalize_config(raw)


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        current = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            current = _normalize_config(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))

        allowed = {
            "exchange",
            "code",
            "mirrorpip_webhook_url",
            "delta_base_url",
            "poll_interval_ms",
            "instrument_type",
        }
        for key, value in updates.items():
            if key == "poll_interval_seconds" and "poll_interval_ms" not in updates:
                try:
                    current["poll_interval_ms"] = max(
                        MIN_POLL_INTERVAL_MS,
                        int(float(value) * 1000),
                    )
                except (TypeError, ValueError):
                    pass
                continue
            if key not in allowed:
                continue
            if key == "poll_interval_ms":
                current[key] = max(MIN_POLL_INTERVAL_MS, int(float(value)))
            else:
                current[key] = str(value).strip() if value is not None else ""

        current = _normalize_config(current)
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
        return current


def load_credentials() -> dict[str, str]:
    with _lock:
        creds = {"key": "", "secret": "", "totp": ""}
        if not CREDENTIALS_PATH.exists():
            return creds

        with CREDENTIALS_PATH.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames and "Title" in reader.fieldnames and "Value" in reader.fieldnames:
                for row in reader:
                    title = (row.get("Title") or "").strip().lower()
                    value = (row.get("Value") or "").strip()
                    if title in creds:
                        creds[title] = value
            else:
                fh.seek(0)
                reader = csv.DictReader(fh)
                for row in reader:
                    for field in creds:
                        if field in row and row[field]:
                            creds[field] = str(row[field]).strip()
                    break
        return creds


def save_credentials(key: str, secret: str, totp: str = "") -> dict[str, str]:
    with _lock:
        key = (key or "").strip()
        secret = (secret or "").strip()
        totp = (totp or "").strip()

        with CREDENTIALS_PATH.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["Title", "Value"])
            writer.writeheader()
            writer.writerow({"Title": "key", "Value": key})
            writer.writerow({"Title": "secret", "Value": secret})
            writer.writerow({"Title": "totp", "Value": totp})

        return {"key": key, "secret": secret, "totp": totp}


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - visible * 2)}{value[-visible:]}"
