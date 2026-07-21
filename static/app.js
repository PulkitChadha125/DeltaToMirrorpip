const $ = (id) => document.getElementById(id);

let logFilters = {
  symbol: "",
  from: "",
  to: "",
  all: false,
};

const logsById = {};

function flash(message, ok = true) {
  const el = $("flash");
  el.textContent = message || "";
  el.className = `flash ${ok ? "ok" : "err"}`;
}

function showPopup({ title, message, detail, ok = true }) {
  const modal = $("confirmModal");
  const icon = $("modalIcon");
  $("modalTitle").textContent = title;
  $("modalMessage").textContent = message;
  icon.textContent = ok ? "✓" : "!";
  icon.className = `modal-icon${ok ? "" : " error"}`;

  const detailEl = $("modalDetail");
  if (detail) {
    detailEl.hidden = false;
    detailEl.textContent =
      typeof detail === "string" ? detail : JSON.stringify(detail, null, 2);
  } else {
    detailEl.hidden = true;
    detailEl.textContent = "";
  }
  modal.hidden = false;
}

function hidePopup() {
  $("confirmModal").hidden = true;
}

$("modalClose").addEventListener("click", hidePopup);
$("confirmModal").addEventListener("click", (e) => {
  if (e.target === $("confirmModal")) hidePopup();
});

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || `Request failed (${res.status})`);
    err.data = data;
    throw err;
  }
  return data;
}

function setStatus(copier) {
  const running = !!copier.running;
  const label = copier.status || (running ? "running" : "stopped");
  $("statusLabel").textContent = label.charAt(0).toUpperCase() + label.slice(1);
  $("statusMeta").textContent = copier.last_error
    ? copier.last_error
    : running
      ? `Watching · ${copier.open_positions || 0} open · ${copier.bindings || 0} mapped`
      : "Idle";

  const dot = $("statusDot");
  dot.className = "dot";
  if (label === "error") dot.classList.add("error");
  else if (running) dot.classList.add("running");

  $("btnStart").disabled = running || label === "logging_in";
  $("btnStop").disabled = !running;
}

function fillConfig(cfg) {
  $("exchange").value = cfg.exchange || "";
  $("code").value = cfg.code || "";
  $("instrument_type").value = cfg.instrument_type || "NA";
  $("poll_interval_ms").value = cfg.poll_interval_ms || 300;
  $("mirrorpip_webhook_url").value = cfg.mirrorpip_webhook_url || "";
  $("delta_base_url").value = cfg.delta_base_url || "";
}

function fillCredentials(creds) {
  $("api_key").value = creds.key || "";
  $("api_secret").value = creds.secret || "";
  $("api_totp").value = creds.totp || "";
}

function fillSymbolFilter(symbols) {
  const select = $("filterSymbol");
  const current = logFilters.symbol || select.value || "";
  select.innerHTML = `<option value="">All symbols</option>`;
  (symbols || []).forEach((sym) => {
    const opt = document.createElement("option");
    opt.value = sym;
    opt.textContent = sym;
    if (sym === current) opt.selected = true;
    select.appendChild(opt);
  });
}

function logsQueryString() {
  const params = new URLSearchParams();
  if (logFilters.symbol) params.set("symbol", logFilters.symbol);
  if (logFilters.from) params.set("from", logFilters.from);
  if (logFilters.to) params.set("to", logFilters.to);
  if (logFilters.all) params.set("all", "1");
  else params.set("limit", "200");
  return params.toString();
}

function pretty(value) {
  if (value === undefined || value === null || value === "") {
    return "—";
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function hideLogDetail() {
  $("logDetailModal").hidden = true;
}

function orderTypeClass(orderType) {
  const t = String(orderType || "").toLowerCase();
  if (t === "buy") return "order-type-buy";
  if (t === "short") return "order-type-short";
  if (t === "sell" || t === "cover") return "order-type-exit";
  return "";
}

async function openLogDetail(logId) {
  let row = logsById[logId];
  if (!row) {
    try {
      const data = await api(`/api/logs/${encodeURIComponent(logId)}`);
      row = data.log;
    } catch (err) {
      flash(err.message || "Could not open log detail", false);
      return;
    }
  }

  const time = (row.time || "").replace("T", " ").replace(/\+00:00$/, "Z");
  const orderType = row.order_type || row.side || "Order";
  const typeClass = orderTypeClass(orderType);
  $("logDetailTitle").innerHTML = typeClass
    ? `<span class="${typeClass}">${escapeHtml(orderType)}</span> · ${escapeHtml(row.symbol || "—")}`
    : `${escapeHtml(orderType)} · ${escapeHtml(row.symbol || "—")}`;
  $("logDetailMeta").textContent =
    `${time} · Delta ${row.delta_order_id || "—"} · Platform ${row.platform_id || "—"} · Mirror ${row.mirror_id || "—"}`;

  $("deltaResponseBox").textContent = pretty(row.delta_response);
  $("mirrorRequestBox").textContent = pretty(row.mirror_request || row.payload);
  $("mirrorResponseBox").textContent = pretty(row.mirror_response);

  $("logDetailModal").hidden = false;
}

function renderLogs(logs) {
  const body = $("logsBody");
  Object.keys(logsById).forEach((k) => delete logsById[k]);

  if (!logs || !logs.length) {
    body.innerHTML = `<tr><td colspan="11" class="empty">No orders yet.</td></tr>`;
    return;
  }

  body.innerHTML = logs
    .map((row, index) => {
      const status = row.status || "info";
      const time = (row.time || "").replace("T", " ").replace(/\+00:00$/, "Z");
      const logId = row.id || `idx-${index}`;
      logsById[logId] = row;
      const clickable = row.status === "copied" || row.status === "error" || row.delta_response || row.mirror_request;
      return `<tr class="${clickable ? "clickable" : ""}" data-log-id="${escapeHtml(logId)}">
        <td>${escapeHtml(time)}</td>
        <td><span class="badge ${escapeHtml(status)}">${escapeHtml(status)}</span></td>
        <td>${escapeHtml(row.delta_order_id || "—")}</td>
        <td>${escapeHtml(row.platform_id || "—")}</td>
        <td>${escapeHtml(row.mirror_id || "—")}</td>
        <td>${escapeHtml(row.symbol || "—")}</td>
        <td>${escapeHtml(row.order_type || row.side || "—")}</td>
        <td>${escapeHtml(row.quantity || "—")}</td>
        <td>${escapeHtml(row.price || "—")}</td>
        <td>${escapeHtml((row.tp || "0") + " / " + (row.sl || "0"))}</td>
        <td class="message">${escapeHtml(row.message || "")}</td>
      </tr>`;
    })
    .join("");
}

$("logsBody").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-log-id]");
  if (!row || !row.classList.contains("clickable")) return;
  openLogDetail(row.getAttribute("data-log-id"));
});

$("logDetailClose").addEventListener("click", hideLogDetail);
$("logDetailModal").addEventListener("click", (e) => {
  if (e.target === $("logDetailModal")) hideLogDetail();
});

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

async function refreshStatus({ syncConfig = false } = {}) {
  const data = await api("/api/status");
  setStatus(data.copier || {});
  if (syncConfig) {
    fillConfig(data.config || {});
  }
}

async function refreshLogs() {
  const qs = logsQueryString();
  const data = await api(`/api/logs?${qs}`);
  fillSymbolFilter(data.symbols || []);
  renderLogs(data.logs || []);
}

async function bootstrap() {
  await refreshStatus({ syncConfig: true });
  const creds = await api("/api/credentials");
  fillCredentials(creds);
  await refreshLogs();
}

$("btnStart").addEventListener("click", async () => {
  $("btnStart").disabled = true;
  flash("Logging in to Delta API...", true);
  try {
    const res = await api("/api/start", { method: "POST", body: "{}" });
    console.log("Delta API login response:", res.login_response || res);
    console.log("Start copy trading response:", res);
    flash(res.message || "Started", true);
    showPopup({
      title: "Copy trading started",
      message: res.message || "Logged in to Delta API successfully. Copy trading started.",
      detail: res.login_response,
      ok: true,
    });
    await refreshStatus();
    await refreshLogs();
  } catch (err) {
    console.error("Delta login / start failed:", err.data || err);
    flash(err.message, false);
    showPopup({
      title: "Login failed",
      message: err.message || "Could not start copy trading.",
      detail: (err.data && (err.data.login_response || err.data)) || null,
      ok: false,
    });
    await refreshStatus().catch(() => {});
  }
});

$("btnStop").addEventListener("click", async () => {
  $("btnStop").disabled = true;
  flash("Stopping copy trading...", true);
  try {
    const res = await api("/api/stop", { method: "POST", body: "{}" });
    console.log("Stop copy trading response:", res);
    flash(res.message || "Stopped", true);
    showPopup({
      title: "Copy trading stopped",
      message: res.message || "Copy trading stopped successfully.",
      detail: res.stop_response,
      ok: true,
    });
    await refreshStatus();
    await refreshLogs();
  } catch (err) {
    console.error("Stop failed:", err.data || err);
    flash(err.message, false);
    showPopup({
      title: "Stop failed",
      message: err.message || "Could not stop copy trading.",
      detail: err.data || null,
      ok: false,
    });
    await refreshStatus().catch(() => {});
  }
});

$("btnTestDelta").addEventListener("click", async () => {
  try {
    const res = await api("/api/test-delta", { method: "POST", body: "{}" });
    console.log("Test Delta API response:", res);
    flash(res.message || "Connected", true);
    showPopup({
      title: "Delta API connected",
      message: res.message || "Connection successful.",
      detail: res.detail || res,
      ok: true,
    });
  } catch (err) {
    console.error("Test Delta failed:", err.data || err);
    flash(err.message, false);
    showPopup({
      title: "Delta API test failed",
      message: err.message,
      detail: err.data || null,
      ok: false,
    });
  }
});

$("btnClearLogs").addEventListener("click", async () => {
  await api("/api/logs/clear", { method: "POST", body: "{}" });
  await refreshLogs();
  flash("Logs cleared", true);
});

$("btnDownloadLogs").addEventListener("click", () => {
  const qs = logsQueryString();
  window.location.href = `/api/logs/csv?${qs}`;
});

$("logFilterForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  logFilters = {
    symbol: $("filterSymbol").value.trim(),
    from: $("filterFrom").value,
    to: $("filterTo").value,
    all: $("filterAll").checked,
  };
  await refreshLogs();
  flash(
    logFilters.all
      ? "Showing all matching logs to date"
      : "Filters applied",
    true
  );
});

$("filterAll").addEventListener("change", () => {
  if ($("filterAll").checked) {
    $("filterFrom").value = "";
    $("filterTo").value = "";
  }
});

$("configForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const webhook = ($("mirrorpip_webhook_url").value || "").trim();
    if (!webhook) {
      flash("Webhook URL is required", false);
      $("mirrorpip_webhook_url").focus();
      return;
    }
    let pollMs = Number($("poll_interval_ms").value || 300);
    if (!Number.isFinite(pollMs) || pollMs < 1) {
      flash("Poll interval must be at least 1 ms", false);
      $("poll_interval_ms").focus();
      return;
    }
    pollMs = Math.floor(pollMs);
    const payload = {
      exchange: $("exchange").value,
      code: $("code").value,
      instrument_type: $("instrument_type").value,
      poll_interval_ms: pollMs,
      mirrorpip_webhook_url: webhook,
      delta_base_url: $("delta_base_url").value,
    };
    const res = await api("/api/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    fillConfig(res.config || payload);
    flash("Config saved", true);
  } catch (err) {
    flash(err.message, false);
  }
});

$("credsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({
        key: $("api_key").value,
        secret: $("api_secret").value,
        totp: $("api_totp").value,
      }),
    });
    flash("Credentials updated in credentials.csv", true);
  } catch (err) {
    flash(err.message, false);
  }
});

bootstrap().catch((err) => flash(err.message, false));
setInterval(() => {
  refreshStatus().catch(() => {});
  refreshLogs().catch(() => {});
}, 2000);
