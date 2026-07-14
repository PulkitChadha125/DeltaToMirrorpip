# Delta → Mirror Pip Copier

Local Python app that watches **positions** on [Delta Exchange](https://docs.delta.exchange/) using the official [`delta-rest-client`](https://pypi.org/project/delta-rest-client/) SDK and copies opens/closes to [Mirror Pip](https://trade.mirrorpip.com) via webhook.

When you press **Start**, the app authenticates with your Delta API credentials, snapshots existing positions (so they are not copied), then polls for size changes. Each change is mapped to a Mirror Pip `order_type` and posted to the TradingView webhook.

## Features

- Start / Stop order copier from a local dashboard
- Editable user config: `exchange`, `code`, webhook URL, Delta API base URL, poll interval
- Update Delta credentials (`credentials.csv`) from the UI
- Order log with Delta ID ↔ Mirror ID binding, payload details, and errors
- Test Delta API connection button

## Requirements

- Python 3.10+
- Delta Exchange API key + secret with **Read** permission (and IP whitelisted)
- Mirror Pip webhook `code`

## Setup

```powershell
cd "D:\Desktop\python projects\DeltaToMirrorpip"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### Credentials

Create or edit `credentials.csv` (also editable from the dashboard):

```csv
Title,Value
key,YOUR_DELTA_API_KEY
secret,YOUR_DELTA_API_SECRET
totp,OPTIONAL_TOTP
```

> **Important:** Whitelist this machine’s public IP on your Delta API key. Without that, requests fail with `ip_not_whitelisted_for_api_key`.

### Config

`config.json` holds Mirror Pip / Delta settings (also editable from the dashboard):

| Field | Description | Default |
| --- | --- | --- |
| `exchange` | Sent in Mirror Pip payload | `delta` |
| `code` | Your Mirror Pip code | — |
| `mirrorpip_webhook_url` | Webhook endpoint | `https://trade.mirrorpip.com/tradingview` |
| `delta_base_url` | Delta REST API | `https://api.india.delta.exchange` |
| `poll_interval_seconds` | How often to poll Delta | `2` |
| `instrument_type` | Sent in payload | `NA` |

## Run

```powershell
.\.venv\Scripts\python.exe app.py
```

Open the dashboard: [http://127.0.0.1:5050](http://127.0.0.1:5050)

1. Confirm / update **exchange** and **code**
2. Confirm Delta credentials
3. Click **Test Delta API**
4. Click **Start Copier**
5. Place an order on Delta — it should appear in the **Order Log** and be sent to Mirror Pip

## How copying works

Uses [`delta-rest-client`](https://pypi.org/project/delta-rest-client/) against Delta REST (`get_all_wallet_balances`, `/v2/positions/margined`, fills, live orders).

1. On start, open Delta positions are snapshotted so they are **not** copied.
2. The worker polls open positions and detects signed size changes.
3. Each change is mapped to a Mirror Pip action and POSTed to the webhook:

| Delta position change | Mirror Pip `order_type` |
| --- | --- |
| Open / increase **long** (buy position) | `buy` |
| Close / reduce **long** | `sell` |
| Open / increase **short** (sell position) | `short` |
| Close / reduce **short** | `cover` |

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
    "code": "YOUR_MIRRORPIP_CODE"
  }
]
```

`order_type` may be `buy`, `sell`, `short`, or `cover`.

### Field mapping

| Mirror Pip field | Source |
| --- | --- |
| `exchange` | Dashboard / `config.json` |
| `code` | Dashboard / `config.json` |
| `chart_symbol` | Delta `product_symbol` |
| `order_type` | From position change mapping above |
| `quantity` | Absolute size of the change |
| `price` | Latest fill price, else position entry price |
| `tp` / `sl` | From related stop/bracket orders when opening; `0` when closing |
| `instrument_type` | Config (`NA` by default) |

Each copied event stores a binding: **event ID → Mirror ID** (shown in the log).

## Project layout

```
DeltaToMirrorpip/
├── app.py                 # Flask dashboard + API
├── copier.py              # Background poll / copy loop
├── delta_client.py        # Wrapper around official delta-rest-client
├── mirrorpip_client.py    # Mirror Pip webhook client
├── config_store.py        # config.json + credentials.csv helpers
├── config.json            # User settings
├── credentials.csv        # Delta API credentials (gitignored)
├── templates/index.html   # Dashboard UI
├── static/                # CSS + JS
└── requirements.txt
```

## Notes

- Credentials and runtime log/binding/position files are gitignored; do not commit API secrets.
- Default Delta base URL is India production (`api.india.delta.exchange`). Change it in the UI if you use another environment.
- Only position changes that occur **after** Start are copied.

## References

- [delta-rest-client on PyPI](https://pypi.org/project/delta-rest-client/)
- [Delta Exchange API docs](https://docs.delta.exchange/)
- Mirror Pip webhook: `https://trade.mirrorpip.com/tradingview`
