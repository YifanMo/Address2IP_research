const state = {
  items: [],
  selectedId: null,
  timer: null,
  configLoaded: false,
};

const elements = {
  statusDot: document.getElementById("status-dot"),
  statusText: document.getElementById("status-text"),
  dnsEndpoint: document.getElementById("dns-endpoint"),
  matchDomains: document.getElementById("match-domains"),
  lastRefresh: document.getElementById("last-refresh"),
  statTotal: document.getElementById("stat-total"),
  statIps: document.getElementById("stat-ips"),
  statDomains: document.getElementById("stat-domains"),
  statLatest: document.getElementById("stat-latest"),
  filterIp: document.getElementById("filter-ip"),
  filterDomain: document.getElementById("filter-domain"),
  filterQtype: document.getElementById("filter-qtype"),
  filterText: document.getElementById("filter-text"),
  refreshBtn: document.getElementById("refresh-btn"),
  clearBtn: document.getElementById("clear-btn"),
  listSubtitle: document.getElementById("list-subtitle"),
  queryList: document.getElementById("query-list"),
  emptyState: document.getElementById("empty-state"),
  detailEmpty: document.getElementById("detail-empty"),
  detailCard: document.getElementById("detail-card"),
  detailQtype: document.getElementById("detail-qtype"),
  detailDomain: document.getElementById("detail-domain"),
  detailIp: document.getElementById("detail-ip"),
  detailPort: document.getElementById("detail-port"),
  detailProtocol: document.getElementById("detail-protocol"),
  detailRcode: document.getElementById("detail-rcode"),
  detailUpstream: document.getElementById("detail-upstream"),
  detailAnswers: document.getElementById("detail-answers"),
  detailDuration: document.getElementById("detail-duration"),
  detailTime: document.getElementById("detail-time"),
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

function rcodeText(value) {
  const names = {
    0: "NOERROR",
    1: "FORMERR",
    2: "SERVFAIL",
    3: "NXDOMAIN",
    5: "REFUSED",
  };
  if (value === null || value === undefined) {
    return "-";
  }
  return `${value} ${names[value] || ""}`.trim();
}

function buildQuery() {
  const params = new URLSearchParams();
  if (elements.filterIp.value.trim()) {
    params.set("ip", elements.filterIp.value.trim());
  }
  if (elements.filterDomain.value.trim()) {
    params.set("domain", elements.filterDomain.value.trim());
  }
  if (elements.filterQtype.value) {
    params.set("qtype", elements.filterQtype.value);
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
  const reportedHost = payload.config?.dns_host || window.location.hostname;
  const dnsHost = reportedHost === "0.0.0.0" || reportedHost === "::"
    ? window.location.hostname
    : reportedHost;
  const dnsPort = payload.config?.dns_port || "53";
  const matches = payload.config?.match_domains || [];
  elements.dnsEndpoint.textContent = `${dnsHost}:${dnsPort}`;
  elements.matchDomains.textContent = matches.length ? matches.join(", ") : "全部域名";
  state.configLoaded = true;
}

function renderSummary(summary) {
  elements.statTotal.textContent = String(summary.total_queries || 0);
  elements.statIps.textContent = String(summary.unique_client_ips || 0);
  elements.statDomains.textContent = String(summary.unique_domains || 0);
  elements.statLatest.textContent = formatTime(summary.latest_timestamp);
}

function renderList() {
  const items = state.items;
  elements.queryList.innerHTML = "";
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
        <span class="method-chip">${escapeHtml(item.qtype || "DNS")}</span>
        <span class="request-id">#${item.id}</span>
        <span class="request-time">${escapeHtml(formatTime(item.timestamp))}</span>
      </div>
      <div class="request-item-main">
        <span class="request-ip">${escapeHtml(item.client_ip)}:${escapeHtml(item.client_port)}</span>
        <span class="flow-arrow">→</span>
        <span class="request-host">${escapeHtml(item.domain || "-")}</span>
      </div>
      <div class="request-preview">
        ${escapeHtml(item.protocol || "udp").toUpperCase()} · ${escapeHtml(item.upstream || "-")} · ${escapeHtml(rcodeText(item.rcode))}
      </div>
    `;
    button.addEventListener("click", () => {
      state.selectedId = item.id;
      renderList();
      renderDetail();
    });
    elements.queryList.appendChild(button);
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
  elements.detailQtype.textContent = selected.qtype || "DNS";
  elements.detailDomain.textContent = selected.domain || "-";
  elements.detailIp.textContent = selected.client_ip || "-";
  elements.detailPort.textContent = String(selected.client_port || "-");
  elements.detailProtocol.textContent = (selected.protocol || "-").toUpperCase();
  elements.detailRcode.textContent = rcodeText(selected.rcode);
  elements.detailUpstream.textContent = selected.upstream || "-";
  elements.detailAnswers.textContent = String(selected.answer_count ?? "-");
  elements.detailDuration.textContent = `${selected.duration_ms || 0} ms`;
  elements.detailTime.textContent = formatTime(selected.timestamp);
  elements.detailJson.textContent = JSON.stringify(selected, null, 2);
}

async function loadQueries() {
  try {
    await loadConfig();
    const response = await fetch(`/api/queries?${buildQuery()}`, { cache: "no-store" });
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

async function clearQueries() {
  const confirmed = window.confirm("确定清空当前 DNS 查询日志吗？");
  if (!confirmed) {
    return;
  }
  const response = await fetch("/api/clear", { method: "POST" });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  state.items = [];
  state.selectedId = null;
  await loadQueries();
}

function attachFilters() {
  const handler = () => {
    window.clearTimeout(state.timer);
    state.timer = window.setTimeout(() => {
      loadQueries();
    }, 250);
  };
  elements.filterIp.addEventListener("input", handler);
  elements.filterDomain.addEventListener("input", handler);
  elements.filterQtype.addEventListener("change", loadQueries);
  elements.filterText.addEventListener("input", handler);
  elements.refreshBtn.addEventListener("click", loadQueries);
  elements.clearBtn.addEventListener("click", async () => {
    try {
      await clearQueries();
    } catch (error) {
      setStatus(false, `清空失败: ${error.message}`);
    }
  });
}

function startPolling() {
  attachFilters();
  loadQueries();
  window.setInterval(loadQueries, 1500);
}

startPolling();
