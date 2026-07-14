"""Delta → Mirror Pip copier dashboard."""

from __future__ import annotations

from flask import Flask, Response, jsonify, render_template, request

from config_store import (
    load_config,
    load_credentials,
    mask_secret,
    save_config,
    save_credentials,
)
from copier import copier
from delta_client import DeltaAPIError, DeltaClient

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    return jsonify(
        {
            "copier": copier.get_status(),
            "config": load_config(),
            "credentials": {
                "key": mask_secret(load_credentials().get("key", "")),
                "secret": mask_secret(load_credentials().get("secret", "")),
                "totp": mask_secret(load_credentials().get("totp", "")),
                "has_key": bool(load_credentials().get("key")),
                "has_secret": bool(load_credentials().get("secret")),
                "has_totp": bool(load_credentials().get("totp")),
            },
        }
    )


@app.get("/api/credentials")
def api_get_credentials():
    return jsonify(load_credentials())


@app.post("/api/credentials")
def api_save_credentials():
    data = request.get_json(silent=True) or {}
    key = data.get("key")
    secret = data.get("secret")
    totp = data.get("totp", "")

    current = load_credentials()
    if key is None or str(key).strip() == "":
        key = current.get("key", "")
    if secret is None or str(secret).strip() == "":
        secret = current.get("secret", "")
    if totp is None:
        totp = current.get("totp", "")

    saved = save_credentials(str(key), str(secret), str(totp))
    return jsonify(
        {
            "ok": True,
            "credentials": {
                "key": mask_secret(saved["key"]),
                "secret": mask_secret(saved["secret"]),
                "totp": mask_secret(saved["totp"]),
            },
        }
    )


@app.post("/api/config")
def api_save_config():
    data = request.get_json(silent=True) or {}
    saved = save_config(data)
    return jsonify({"ok": True, "config": saved})


@app.post("/api/start")
def api_start():
    result = copier.start()
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


@app.post("/api/stop")
def api_stop():
    result = copier.stop()
    status = 200 if result.get("ok") else 400
    return jsonify(result), status


def _log_filters_from_request() -> dict:
    return {
        "symbol": request.args.get("symbol") or None,
        "date_from": request.args.get("from") or request.args.get("date_from") or None,
        "date_to": request.args.get("to") or request.args.get("date_to") or None,
    }


@app.get("/api/logs/<log_id>")
def api_log_detail(log_id: str):
    row = copier.get_log(log_id)
    if not row:
        return jsonify({"ok": False, "message": "Log not found."}), 404
    return jsonify({"ok": True, "log": row})


@app.get("/api/logs")
def api_logs():
    filters = _log_filters_from_request()
    all_logs = request.args.get("all", "0") in {"1", "true", "yes"}
    limit = None if all_logs else request.args.get("limit", 200, type=int)
    logs = copier.get_logs(limit=limit, **filters)
    return jsonify(
        {
            "logs": logs,
            "count": len(logs),
            "symbols": copier.get_log_symbols(),
            "filters": filters,
        }
    )


@app.get("/api/logs/csv")
def api_logs_csv():
    filters = _log_filters_from_request()
    csv_text = copier.logs_to_csv(**filters)
    filename = "order_logs.csv"
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.post("/api/logs/clear")
def api_clear_logs():
    copier.clear_logs()
    return jsonify({"ok": True})


@app.post("/api/test-delta")
def api_test_delta():
    creds = load_credentials()
    cfg = load_config()
    if not creds.get("key") or not creds.get("secret"):
        return jsonify({"ok": False, "message": "Missing API key/secret."}), 400
    try:
        client = DeltaClient(creds["key"], creds["secret"], cfg["delta_base_url"])
        info = client.test_connection()
        return jsonify({"ok": True, "message": "Delta API connected.", "detail": info})
    except DeltaAPIError as exc:
        return jsonify({"ok": False, "message": str(exc), "detail": exc.payload}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
