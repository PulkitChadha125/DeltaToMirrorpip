# Delta → Mirror Pip Copier

Local / server Python app that watches **net positions** on [Delta Exchange](https://docs.delta.exchange/) using the official [`delta-rest-client`](https://pypi.org/project/delta-rest-client/) SDK and copies opens/closes to [Mirror Pip](https://trade.mirrorpip.com) via webhook.

When you click **Start**, the app:

1. Logs in to the Delta API (using `credentials.csv`)
2. Snapshots existing open positions (those are **not** copied)
3. Polls net position size on a fast interval (default **300 ms**)
4. On each change, maps **Delta order ↔ platform ID ↔ Mirror Pip ID**, sends the Mirror Pip webhook, and writes an order log

## Features

- Start / Stop copy trading from a web dashboard
- Editable config: `exchange`, `code`, webhook URL, Delta base URL, poll interval (ms)
- Edit / save Delta credentials (`credentials.csv`) from the UI
- Position mapping: Delta ID ↔ platform ID ↔ Mirror ID (exits close the matching mapped trade)
- Order log with filters (symbol, date range, all logs to date) and **CSV download**
- Click a log row to view Delta response, Mirror Pip request, Mirror Pip response, and latency
- `run.bat` for one-click setup + start
- `open_port_5050.bat` to allow inbound TCP 5050 on Windows Firewall

## Requirements

### System

- Windows (batch scripts) or any OS with Python
- **Python 3.10+** on PATH
- Network access to Delta Exchange API and Mirror Pip webhook
- Delta API key with **Read** permission and **your server IP whitelisted**

### Python packages

Install from `requirements.txt`:

| Package | Purpose |
| --- | --- |
| `Flask` | Web dashboard |
| `requests` | HTTP calls (also used by SDK) |
| `delta-rest-client` | Official Delta Exchange REST SDK |
| `Werkzeug`, `Jinja2`, `click`, `blinker`, `itsdangerous`, `MarkupSafe`, `colorama` | Flask stack |
| `urllib3`, `certifi`, `charset-normalizer`, `idna` | requests stack |

```powershell
pip install -r requirements.txt
```

### External accounts

- Delta Exchange API **key** + **secret** (optional TOTP stored but not required for REST signing)
- Mirror Pip **code** and webhook URL

## How to run

### Option A — REST polling (default)

1. Put your keys in `credentials.csv` (or fill them in the UI after start).
2. Double-click **`run.bat`**
   - Creates `.venv` if missing
   - Installs `requirements.txt`
   - Starts the app on **port 5050** (bound to `0.0.0.0`)
3. Open the dashboard:
   - Local: http://127.0.0.1:5050
   - Remote: http://YOUR-SERVER-IP:5050

### Option A2 — WebSocket positions (same UI)

Use **`run_ws.bat`** instead of `run.bat`. The dashboard looks the same; position updates come from Delta’s private **positions** WebSocket channel.

**WebSocket flow:**
1. Connect to `wss://socket.india.delta.exchange`
2. Authenticate with `key-auth` (HMAC signature)
3. Enable heartbeat
4. Subscribe to `positions` with `"symbols": ["all"]`
5. Receive initial snapshot (not copied — baseline already set via REST)
6. Receive incremental `create` / `update` / `delete` events → Mirror Pip copy

Enable via `run_ws.bat`, `"position_source": "websocket"` in `config.json`, or `DELTA_POSITION_SOURCE=websocket`.

### Option B — open firewall port (server access)

To allow other machines to reach the dashboard on port 5050:

1. Right-click **`open_port_5050.bat`** → **Run as administrator** (or allow the UAC prompt)
2. It adds a Windows Firewall inbound rule for TCP **5050**
3. Start the app with `run.bat`

### Option C — manual

```powershell
cd "D:\Desktop\python projects\DeltaToMirrorpip"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open http://127.0.0.1:5050

### Dashboard steps

1. Set **Exchange**, **Code**, and **Webhook URL** → Save Config  
2. Confirm API key / secret → Update Credentials  
3. (Optional) **Test Delta API**  
4. Click **Start Copier** — login response shows in a popup and in the console  
5. Trade on Delta — opens/closes appear in **Order Log** and are sent to Mirror Pip  
6. Click **Stop** to stop copy trading  

## Credentials

`credentials.csv` (also editable from the UI):

```csv
Title,Value
key,YOUR_DELTA_API_KEY
secret,YOUR_DELTA_API_SECRET
totp,OPTIONAL_TOTP
```

> **Important:** Whitelist this machine’s public IP on your Delta API key. Without that, login fails with `ip_not_whitelisted_for_api_key`.

## Config

`config.json` (also editable from the dashboard):

| Field | Description | Default |
| --- | --- | --- |
| `exchange` | Sent in Mirror Pip payload | `delta` |
| `code` | Your Mirror Pip code | — |
| `mirrorpip_webhook_url` | Webhook endpoint | `https://trade.mirrorpip.com/tradingview` |
| `delta_base_url` | Delta REST API | `https://api.india.delta.exchange` |
| `delta_ws_url` | Delta private WebSocket URL (optional; derived from REST URL if blank) | — |
| `poll_interval_ms` | REST poll interval (REST mode only) | `300` |
| `instrument_type` | Sent in payload | `NA` |
| `position_source` | `rest` or `websocket` | `rest` |

## How copying works

Uses [`delta-rest-client`](https://pypi.org/project/delta-rest-client/) to:

- Authenticate (wallet/balances check)
- Poll `/v2/positions/margined` for **net position** size (**REST mode**), or subscribe to the private **positions** WebSocket channel (**WebSocket mode** via `websocket.py`)
- Read recent fills / live orders for price and TP/SL context

### Order type mapping

| Delta position change | Mirror Pip `order_type` |
| --- | --- |
| Open / increase **long** | `buy` |
| Close / reduce **long** | `sell` |
| Open / increase **short** | `short` |
| Close / reduce **short** | `cover` |

### ID mapping

When a position opens:

`Delta order ID` ↔ **platform ID** (generated locally) ↔ **Mirror Pip ID** (from response when available)

When that position is reduced/closed on Delta, the matching mapped trade is exited on Mirror Pip (`sell` or `cover`) and logged.

### Example webhook payload

```json
[
  {
    "exchange": "delta",
    "price": "59000",
    "chart_symbol": "BTCUSD",
    "order_type": "buy",
    "instrument_type": "NA",
    "quantity": "100",
    "tp": "20",
    "sl": "10",
    "code": "YOUR_MIRRORPIP_CODE",
    "platform_id": "101"
  }
]
```

| Field | Source |
| --- | --- |
| `exchange` / `code` / `instrument_type` | Dashboard / `config.json` |
| `chart_symbol` | Delta `product_symbol` |
| `order_type` | Mapping table above (`buy` / `sell` / `short` / `cover`) |
| `quantity` | Absolute size of the net-position change |
| `price` | Latest fill price, else position entry price |
| `tp` / `sl` | Related stop/bracket orders when opening; `0` when closing |
| `platform_id` | Internal mapping ID |

## Order log

- Filter by **symbol**, **from/to date**, or **all logs to date**
- **Download CSV** uses the active filters
- Click a row to inspect:
  - Delta response
  - Mirror Pip request
  - Mirror Pip response
  - Overall latency (ms)

## Project layout

```
DeltaToMirrorpip/
├── app.py                 # Flask dashboard + API (listens on 0.0.0.0:5050)
├── copier.py              # Position poll / copy / mapping loop
├── delta_client.py        # Wrapper around delta-rest-client
├── mirrorpip_client.py    # Mirror Pip webhook client
├── config_store.py        # config.json + credentials.csv helpers
├── config.json            # User settings
├── credentials.csv        # Delta API credentials (gitignored)
├── run.bat                # Create venv, install deps, start app
├── open_port_5050.bat     # Open Windows Firewall for TCP 5050
├── requirements.txt       # Python dependencies
├── templates/index.html   # Dashboard UI
├── static/                # CSS + JS
└── README.md
```

## Notes

- Do not commit API secrets. `credentials.csv` and runtime files (`order_logs.json`, bindings, snapshots) are gitignored.
- Default Delta base URL is India production (`https://api.india.delta.exchange`). Change it in the UI for testnet/global if needed.
- Only position changes that occur **after** Start are copied.

## References

- [delta-rest-client on PyPI](https://pypi.org/project/delta-rest-client/)
- [Delta Exchange API docs](https://docs.delta.exchange/)
- Mirror Pip webhook: `https://trade.mirrorpip.com/tradingview`
