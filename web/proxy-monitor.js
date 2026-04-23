const state = {
  items: [],
  selectedId: null,
  timer: null,
  configLoaded: false,
};

const elements = {
  statusDot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  proxyEndpoint: document.getElementById("proxy-endpoint"),
  dashboardApi: document.getElementById("dashboard-api"),
  lastRefresh: document.getElementById("last-refresh"),
  statTotal: document.getElementById("stat-total"),
  statIps: document.getElementById("stat-ips"),
  statHosts: document.getElementById("stat-hosts"),
  statLatest: document.getElementById("stat-latest"),
  filterIp: document.getElementById("filter-ip"),
  filterHost: document.getElementById("filter-host"),
  filterMode: document.getElementById("filter-mode"),
  filterText: document.getElementById("filter-text"),
  refreshBtn: document.getElementById("refresh-btn"),
  clearBtn: document.getElementById("clear-btn"),
  listSubtitle: document.getElementById("list-subtitle"),
  hitList: document.getElementById("hit-list"),
  emptyState: document.getElementById("empty-state"),
  detailEmpty: document.getElementById("detail-empty"),
  detailCard: document.getElementById("detail-card"),
  detailMode: document.getElementById("detail-mode"),
  detailTarget: document.getElementById("detail-target"),
  detailIp: document.getElementById("detail-ip"),
  detailPort: document.getElementById("detail-port"),
  detailMethod: document.getElementById("detail-method"),
  detailStatus: document.getElementById("detail-status"),
  detailHost: document.getElementById("detail-host"),
  detailDuration: document.getElementById("detail-duration"),
  detailRequestBytes: document.getElementById("detail-request-bytes"),
  detailResponseBytes: document.getElementById("detail-response-bytes"),
  detailNote: document.getElementById("detail-note"),
  detailJson: document.getElementById("detail-json"),
};

function setStatus(connected, text) {
  elements.statusDot.classList.toggle("online", connected);
  elements.statusText.textContent = text;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(value) {
  if (!value) {
    return "暂无";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "short",
    timeStyle: "medium",
  }).format(new Date(value));
}

function formatBytes(value) {
  const numeric = Number(value || 0);
  if (!Number.isFinite(numeric) || numeric < 0) {
    return "-";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = numeric;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return `${amount.toFixed(2)} ${units[unitIndex]}`;
}

function modeLabel(mode) {
  return mode === "https-connect" ? "HTTPS" : "HTTP";
}

function modeClass(mode) {
  return mode === "https-connect" ? "connect-chip" : "";
}

function targetText(item) {
  if (item.url) {
    return item.url;
  }
  const host = item.host || "-";
  const port = item.port ? `:${item.port}` : "";
  return `${host}${port}`;
}

function summaryText(item) {
  if (item.mode === "https-connect") {
    return `${item.client_to_server_bytes || 0} B up / ${item.server_to_client_bytes || 0} B down · ${item.duration_ms || 0} ms`;
  }
  return `${item.method || "GET"} · HTTP ${item.response_status || "-"} · ${item.duration_ms || 0} ms`;
}

function buildQuery() {
  const params = new URLSearchParams();
  if (elements.filterIp.value.trim()) {
    params.set("ip", elements.filterIp.value.trim());
  }
  if (elements.filterHost.value.trim()) {
    params.set("host", elements.filterHost.value.trim());
  }
  if (elements.filterMode.value) {
    params.set("mode", elements.filterMode.value);
  }
  if (elements.filterText.value.trim()) {
    params.set("text", elements.filterText.value.trim());
  }
  params.set("limit", "200");
  return params.toString();
}

async function loadConfig() {
  if (state.configLoaded) {
    return;
  }
  const response = await fetch("/api/config", { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  const payload = await response.json();
  const reportedProxyHost = payload.config?.proxy_host || window.location.hostname;
  const proxyHost = reportedProxyHost === "0.0.0.0" || reportedProxyHost === "::"
    ? window.location.hostname
    : reportedProxyHost;
  const proxyPort = payload.config?.proxy_port || "18090";
  elements.proxyEndpoint.textContent = `${proxyHost}:${proxyPort}`;
  elements.dashboardApi.textContent = `${window.location.origin}/api/hits`;
  state.configLoaded = true;
}

function renderSummary(summary) {
  elements.statTotal.textContent = String(summary.total_hits || 0);
  elements.statIps.textContent = String(summary.unique_client_ips || 0);
  elements.statHosts.textContent = String(summary.unique_hosts || 0);
  elements.statLatest.textContent = formatTime(summary.latest_timestamp);
}

function renderList() {
  const items = state.items;
  elements.hitList.innerHTML = "";
  elements.emptyState.classList.toggle("hidden", items.length > 0);
  elements.listSubtitle.textContent = items.length
    ? `当前筛选结果 ${items.length} 条`
    : "没有匹配结果";

  for (const item of items) {
    const button = document.createElement("button");
    button.className = "request-item";
    if (item.id === state.selectedId) {
      button.classList.add("active");
    }

    button.innerHTML = `
      <div class="request-item-top">
        <span class="method-chip ${modeClass(item.mode)}">${escapeHtml(modeLabel(item.mode))}</span>
        <span class="request-id">#${item.id}</span>
        <span class="request-time">${escapeHtml(formatTime(item.timestamp))}</span>
      </div>
      <div class="request-item-main">
        <span class="request-ip">${escapeHtml(item.client_ip)}:${escapeHtml(item.client_port)}</span>
        <span class="flow-arrow">→</span>
        <span class="request-host">${escapeHtml(item.host || "-")}:${escapeHtml(item.port || "-")}</span>
      </div>
      <div class="request-preview">${escapeHtml(targetText(item))}</div>
      <div class="request-preview request-preview-meta">${escapeHtml(summaryText(item))}</div>
    `;
    button.addEventListener("click", () => {
      state.selectedId = item.id;
      renderList();
      renderDetail();
    });
    elements.hitList.appendChild(button);
  }
}

function renderDetail() {
  const selected = state.items.find((item) => item.id === state.selectedId) || state.items[0];
  if (!selected) {
    state.selectedId = null;
    elements.detailEmpty.classList.remove("hidden");
    elements.detailCard.classList.add("hidden");
    return;
  }

  state.selectedId = selected.id;
  elements.detailEmpty.classList.add("hidden");
  elements.detailCard.classList.remove("hidden");
  elements.detailMode.textContent = modeLabel(selected.mode);
  elements.detailMode.className = `method-chip ${modeClass(selected.mode)}`.trim();
  elements.detailTarget.textContent = targetText(selected);
  elements.detailIp.textContent = selected.client_ip || "-";
  elements.detailPort.textContent = String(selected.client_port || "-");
  elements.detailMethod.textContent = selected.method || "-";
  elements.detailStatus.textContent = selected.response_status ? `HTTP ${selected.response_status}` : "-";
  elements.detailHost.textContent = selected.host ? `${selected.host}:${selected.port || ""}` : "-";
  elements.detailDuration.textContent = `${selected.duration_ms || 0} ms`;
  elements.detailRequestBytes.textContent = formatBytes(
    selected.request_body_bytes ?? selected.client_to_server_bytes ?? 0
  );
  elements.detailResponseBytes.textContent = formatBytes(
    selected.response_body_bytes ?? selected.server_to_client_bytes ?? 0
  );
  elements.detailNote.textContent = selected.note || (selected.mode === "https-connect"
    ? "HTTPS CONNECT 只能看到目标主机和端口，看不到加密后的路径。"
    : "HTTP 代理模式可以看到完整 URL 和响应状态。");
  elements.detailJson.textContent = JSON.stringify(selected, null, 2);
}

async function loadHits() {
  try {
    await loadConfig();
    const response = await fetch(`/api/hits?${buildQuery()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    const payload = await response.json();
    state.items = payload.items || [];
    if (!state.items.some((item) => item.id === state.selectedId)) {
      state.selectedId = state.items[0]?.id || null;
    }
    renderSummary(payload.summary || {});
    renderList();
    renderDetail();
    elements.lastRefresh.textContent = formatTime(new Date().toISOString());
    setStatus(true, "已连接，正在自动刷新");
  } catch (error) {
    setStatus(false, `连接失败: ${error.message}`);
  }
}

async function clearHits() {
  const confirmed = window.confirm("确定清空当前代理命中日志吗？");
  if (!confirmed) {
    return;
  }
  const response = await fetch("/api/clear", { method: "POST" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  state.items = [];
  state.selectedId = null;
  await loadHits();
}

function attachFilters() {
  const handler = () => {
    window.clearTimeout(state.timer);
    state.timer = window.setTimeout(() => {
      loadHits();
    }, 250);
  };
  elements.filterIp.addEventListener("input", handler);
  elements.filterHost.addEventListener("input", handler);
  elements.filterMode.addEventListener("change", loadHits);
  elements.filterText.addEventListener("input", handler);
  elements.refreshBtn.addEventListener("click", loadHits);
  elements.clearBtn.addEventListener("click", async () => {
    try {
      await clearHits();
    } catch (error) {
      setStatus(false, `清空失败: ${error.message}`);
    }
  });
}

function startPolling() {
  attachFilters();
  loadHits();
  window.setInterval(loadHits, 1500);
}

startPolling();
