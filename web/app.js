const state = {
  items: [],
  selectedId: null,
  timer: null,
};

const elements = {
  statusDot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  captureEndpoint: document.getElementById("capture-endpoint"),
  lastRefresh: document.getElementById("last-refresh"),
  statTotal: document.getElementById("stat-total"),
  statIps: document.getElementById("stat-ips"),
  statLatest: document.getElementById("stat-latest"),
  statMethods: document.getElementById("stat-methods"),
  filterIp: document.getElementById("filter-ip"),
  filterMethod: document.getElementById("filter-method"),
  filterText: document.getElementById("filter-text"),
  refreshBtn: document.getElementById("refresh-btn"),
  clearBtn: document.getElementById("clear-btn"),
  listSubtitle: document.getElementById("list-subtitle"),
  requestList: document.getElementById("request-list"),
  emptyState: document.getElementById("empty-state"),
  detailEmpty: document.getElementById("detail-empty"),
  detailCard: document.getElementById("detail-card"),
  detailMethod: document.getElementById("detail-method"),
  detailPath: document.getElementById("detail-path"),
  detailIp: document.getElementById("detail-ip"),
  detailPort: document.getElementById("detail-port"),
  detailTime: document.getElementById("detail-time"),
  detailSize: document.getElementById("detail-size"),
  detailHeaders: document.getElementById("detail-headers"),
  detailBody: document.getElementById("detail-body"),
  detailJson: document.getElementById("detail-json"),
};

function setStatus(connected, text) {
  elements.statusDot.classList.toggle("online", connected);
  elements.statusText.textContent = text;
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

function formatMethods(methods) {
  const entries = Object.entries(methods || {});
  if (!entries.length) {
    return "暂无";
  }
  return entries.map(([name, count]) => `${name} ${count}`).join(" / ");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function buildQuery() {
  const params = new URLSearchParams();
  if (elements.filterIp.value.trim()) {
    params.set("ip", elements.filterIp.value.trim());
  }
  if (elements.filterMethod.value) {
    params.set("method", elements.filterMethod.value);
  }
  if (elements.filterText.value.trim()) {
    params.set("text", elements.filterText.value.trim());
  }
  params.set("limit", "200");
  return params.toString();
}

function requestPreview(item) {
  const body = item.body ? item.body.slice(0, 120) : "无 body";
  return body || "无 body";
}

function renderSummary(summary) {
  elements.statTotal.textContent = String(summary.total_requests || 0);
  elements.statIps.textContent = String(summary.unique_client_ips || 0);
  elements.statLatest.textContent = formatTime(summary.latest_timestamp);
  elements.statMethods.textContent = formatMethods(summary.methods);
  elements.captureEndpoint.textContent = `${window.location.origin}/demo`;
}

function renderList() {
  const items = state.items;
  elements.requestList.innerHTML = "";
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

    const preview = escapeHtml(requestPreview(item));
    const path = escapeHtml(item.raw_path);
    const ip = escapeHtml(item.client_ip);
    button.innerHTML = `
      <div class="request-item-top">
        <span class="method-chip">${escapeHtml(item.method)}</span>
        <span class="request-id">#${item.id}</span>
        <span class="request-time">${escapeHtml(formatTime(item.timestamp))}</span>
      </div>
      <div class="request-item-main">
        <span class="request-ip">${ip}:${escapeHtml(item.client_port)}</span>
        <span class="request-path">${path}</span>
      </div>
      <div class="request-preview">${preview}</div>
    `;
    button.addEventListener("click", () => {
      state.selectedId = item.id;
      renderList();
      renderDetail();
    });
    elements.requestList.appendChild(button);
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
  elements.detailMethod.textContent = selected.method;
  elements.detailPath.textContent = selected.raw_path;
  elements.detailIp.textContent = selected.client_ip;
  elements.detailPort.textContent = String(selected.client_port);
  elements.detailTime.textContent = formatTime(selected.timestamp);
  elements.detailSize.textContent = `${selected.body_length} bytes`;
  elements.detailHeaders.textContent = JSON.stringify(selected.headers, null, 2);
  elements.detailBody.textContent = selected.body || "(empty)";
  elements.detailJson.textContent = JSON.stringify(selected, null, 2);
}

async function loadRequests() {
  try {
    const response = await fetch(`/api/requests?${buildQuery()}`, { cache: "no-store" });
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

async function clearRequests() {
  const confirmed = window.confirm("确定清空当前所有已记录请求吗？");
  if (!confirmed) {
    return;
  }
  const response = await fetch("/api/clear", {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  state.items = [];
  state.selectedId = null;
  await loadRequests();
}

function attachFilters() {
  const handler = () => {
    window.clearTimeout(state.timer);
    state.timer = window.setTimeout(() => {
      loadRequests();
    }, 250);
  };
  elements.filterIp.addEventListener("input", handler);
  elements.filterMethod.addEventListener("change", loadRequests);
  elements.filterText.addEventListener("input", handler);
  elements.refreshBtn.addEventListener("click", loadRequests);
  elements.clearBtn.addEventListener("click", async () => {
    try {
      await clearRequests();
    } catch (error) {
      setStatus(false, `清空失败: ${error.message}`);
    }
  });
}

function startPolling() {
  attachFilters();
  loadRequests();
  window.setInterval(loadRequests, 1500);
}

startPolling();
