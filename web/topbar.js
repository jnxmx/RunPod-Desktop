(() => {
  if (window.__RUNPOD_DESKTOP_TOPBAR_LOADED__) return;
  window.__RUNPOD_DESKTOP_TOPBAR_LOADED__ = true;

  let autoDownloadEnabled = true;

  // Forward completed pod outputs through Comfy Desktop's native download manager
  // so files appear in the Electron header DOWNLOADS popup with progress and thumbnails.
  if (
    !window.__rpNativeAutoDownloadInstalled &&
    window.__comfyDesktop2 &&
    typeof window.__comfyDesktop2.downloadAsset === "function" &&
    typeof window.WebSocket === "function"
  ) {
    window.__rpNativeAutoDownloadInstalled = true;
    const _handledOutputs = Object.create(null);

    function _buildViewUrl(baseUrl, item) {
      let params = "filename=" + encodeURIComponent(item.filename);
      if (item.subfolder) params += "&subfolder=" + encodeURIComponent(item.subfolder);
      if (item.type) params += "&type=" + encodeURIComponent(item.type);
      return `${baseUrl}/api/view?${params}`;
    }

    function _withSubfolder(subfolder, name) {
      if (!subfolder) return name;
      if (name.indexOf(subfolder + "/") === 0) return name;
      return `${subfolder}/${name}`;
    }

    function _downloadOutputItem(baseUrl, authToken, eventKey, item) {
      if (!item || typeof item !== "object") return;
      const filename = typeof item.filename === "string" ? item.filename.trim() : "";
      if (!filename) return;
      const rawType = typeof item.type === "string" ? item.type.trim() : "";
      const normType = rawType.toLowerCase();
      if (normType === "temp" || normType === "input") return;
      const subfolder = typeof item.subfolder === "string" ? item.subfolder.trim() : "";
      const dedupeKey = `${eventKey}|${subfolder}|${filename}|${rawType}`;
      if (_handledOutputs[dedupeKey]) return;
      _handledOutputs[dedupeKey] = true;
      const preferredName =
        typeof item.display_name === "string" && item.display_name.trim()
          ? item.display_name.trim()
          : filename;
      const saveName = _withSubfolder(subfolder, preferredName);
      const viewUrl = _buildViewUrl(baseUrl, { filename, subfolder, type: rawType });
      try {
        Promise.resolve(
          window.__comfyDesktop2.downloadAsset(viewUrl, saveName, authToken || "")
        ).catch(() => {});
      } catch {}
    }

    const OrigWebSocket = window.WebSocket;
    function WrappedWebSocket(url, protocols) {
      const ws =
        protocols !== undefined
          ? new OrigWebSocket(url, protocols)
          : new OrigWebSocket(url);
      const wsUrl = typeof url === "string" ? url : String(url);
      let resolvedWsUrl = wsUrl;
      let authToken = null;
      let pathname = "";
      try {
        const baseHref =
          window.location && window.location.href ? window.location.href : "http://127.0.0.1/";
        const urlObj = new URL(wsUrl, baseHref);
        resolvedWsUrl = urlObj.href;
        authToken = urlObj.searchParams.get("token");
        pathname = urlObj.pathname || "";
      } catch {}
      if (!pathname.startsWith("/api/agent/")) {
        const httpBase = resolvedWsUrl.replace(/^ws(s?):/, "http$1:").replace(/\/ws(\?.*)?$/, "");

        ws.addEventListener("message", (event) => {
          if (!autoDownloadEnabled || typeof event.data !== "string") return;
          if (!event.data.includes('"executed"')) return;
          let msg;
          try {
            msg = JSON.parse(event.data);
          } catch {
            return;
          }
          if (!msg || msg.type !== "executed" || !msg.data || !msg.data.output || typeof msg.data.output !== "object") {
            return;
          }
          const output = msg.data.output;
          const eventKey = `${msg.data.prompt_id || ""}|${msg.data.node || ""}`;
          for (const items of Object.values(output)) {
            if (!Array.isArray(items) || items.length === 0) continue;
            for (const item of items) {
              _downloadOutputItem(httpBase, authToken, eventKey, item);
            }
          }
        });
      }

      return ws;
    }

    WrappedWebSocket.prototype = OrigWebSocket.prototype;
    Object.setPrototypeOf(WrappedWebSocket, OrigWebSocket);
    for (const staticKey of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
      if (staticKey in OrigWebSocket) {
        WrappedWebSocket[staticKey] = OrigWebSocket[staticKey];
      }
    }
    window.WebSocket = WrappedWebSocket;
  }

  // Prevent Comfy Desktop's local terminal stopgap from registering over the remote pod terminal
  if (!window.__comfyDesktopTerminalStopgap) {
    window.__comfyDesktopTerminalStopgap = { registered: true, tabBarInjectorStarted: true };
  } else {
    window.__comfyDesktopTerminalStopgap.registered = true;
    window.__comfyDesktopTerminalStopgap.tabBarInjectorStarted = true;
    try { window.__comfyDesktopTerminalStopgap.tabBarObserver?.disconnect(); } catch {}
  }

  const DEFAULT_BOTTOM = 8;

  const host = document.createElement("div");
  host.id = "rp-desktop-topbar-host";
  host.style.cssText =
    `position:fixed;bottom:${DEFAULT_BOTTOM}px;left:50%;transform:translateX(-50%);z-index:2147483647;pointer-events:none;`;
  document.documentElement.appendChild(host);

  const shadow = host.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = `
    :host {
      all: initial;
      font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    *, *::before, *::after {
      box-sizing: border-box;
    }
    [hidden] {
      display: none !important;
    }
    .wrap {
      position: relative;
      pointer-events: auto;
      display: inline-block;
    }
    .bar {
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      padding: 4px 12px;
      border-radius: 16px;
      background: var(--background, #161617);
      border: none;
      box-shadow: var(--shadow-inset-highlight, inset 0 1px 0 0 #ffffff1a), 0 2px 16px 0 var(--shadow-color, #0006);
      color: #e4e4e7;
      font-size: 12px;
      line-height: 1;
      user-select: none;
      transition: padding 0.36s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .wrap.is-expanded .bar,
    .wrap.is-confirming .bar {
      padding: 6px 10px 7px;
    }
    .pill-btn {
      height: 24px;
      padding: 0 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      background: transparent;
      border: none;
      border-radius: 999px;
      color: #a1a1aa;
      font-family: inherit;
      font-size: 11.5px;
      font-weight: 500;
      cursor: pointer;
      flex-shrink: 0;
      white-space: nowrap;
      transition: color 0.16s ease, background-color 0.16s ease;
    }
    .pill-btn:hover:not(:disabled) {
      background: var(--secondary, rgba(255, 255, 255, 0.08));
      color: #f4f4f5;
    }
    .pill-btn:disabled {
      opacity: 0.45;
      cursor: not-allowed;
    }
    .pill-btn svg {
      flex-shrink: 0;
    }
    .pill-btn.is-busy {
      animation: rp-pulse 0.9s ease-in-out infinite;
    }
    .actions-drawer {
      display: grid;
      grid-template-rows: 0fr;
      max-width: 0;
      opacity: 0;
      margin-top: 0;
      padding-top: 0;
      border-top: 1px solid transparent;
      overflow: hidden;
      transition:
        grid-template-rows 0.36s cubic-bezier(0.4, 0, 0.2, 1),
        max-width 0.38s cubic-bezier(0.4, 0, 0.2, 1),
        opacity 0.28s cubic-bezier(0.4, 0, 0.2, 1),
        margin-top 0.36s cubic-bezier(0.4, 0, 0.2, 1),
        padding-top 0.36s cubic-bezier(0.4, 0, 0.2, 1),
        border-top-color 0.36s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .wrap.is-expanded .actions-drawer,
    .wrap.is-confirming .actions-drawer {
      grid-template-rows: 1fr;
      max-width: 440px;
      opacity: 1;
      margin-top: 5px;
      padding-top: 5px;
      border-top-color: rgba(255, 255, 255, 0.07);
    }
    .actions-inner {
      min-height: 0;
      overflow: hidden;
    }
    .actions-row {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 4px;
      white-space: nowrap;
    }
    .status-wing {
      display: inline-block;
      max-width: 0;
      opacity: 0;
      overflow: hidden;
      white-space: nowrap;
      color: #a1a1aa;
      font-weight: 500;
      font-variant-numeric: tabular-nums;
      transition: max-width 0.36s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .status-left {
      text-align: right;
    }
    .status-right {
      text-align: left;
    }
    .wrap.is-expanded .status-wing,
    .wrap.is-confirming .status-wing {
      max-width: 110px;
      opacity: 1;
    }
    .pods-badge {
      display: inline-flex;
      align-items: center;
      height: 18px;
      padding: 0 7px;
      margin-right: 8px;
      border-radius: 999px;
      background: rgba(245, 158, 11, 0.14);
      border: 1px solid rgba(245, 158, 11, 0.28);
      color: #fbbf24;
      font-size: 11px;
      font-weight: 600;
      line-height: 1;
      font-variant-numeric: tabular-nums;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .pods-badge-dot {
      width: 5px;
      height: 5px;
      border-radius: 999px;
      background: #f59e0b;
      margin-right: 5px;
      flex-shrink: 0;
    }
    .pods-badge-spend {
      display: inline-block;
      max-width: 0;
      opacity: 0;
      overflow: hidden;
      white-space: nowrap;
      font-weight: 500;
      color: rgba(251, 191, 36, 0.88);
      transition: max-width 0.36s cubic-bezier(0.4, 0, 0.2, 1), opacity 0.28s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .wrap.is-expanded .pods-badge-spend,
    .wrap.is-confirming .pods-badge-spend {
      max-width: 90px;
      opacity: 1;
    }
    .pill-btn-danger {
      color: #f87171;
    }
    .pill-btn-danger:hover:not(:disabled) {
      background: rgba(248, 113, 113, 0.14);
      color: #fca5a5;
    }
    .pill-btn-danger-solid {
      background: rgba(239, 68, 68, 0.22);
      color: #fecaca;
      padding: 0 8px;
    }
    .pill-btn-danger-solid:hover:not(:disabled) {
      background: rgba(239, 68, 68, 0.34);
      color: #ffffff;
    }
    .confirm-box {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 0 2px;
    }
    .confirm-text {
      font-size: 11.5px;
      font-weight: 500;
      color: #fca5a5;
      white-space: nowrap;
      padding-right: 4px;
    }
    @keyframes rp-pulse {
      0%, 100% { opacity: 0.85; }
      50% { opacity: 0.35; }
    }
    .status {
      width: 100%;
      height: 24px;
      padding: 0 4px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      color: #f4f4f5;
      font-family: inherit;
      font-size: 12px;
      font-weight: 500;
      white-space: nowrap;
      cursor: default;
    }
    .status-center {
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .gpu-name {
      color: #f4f4f5;
      font-weight: 500;
    }
    .spent {
      display: inline-flex;
      align-items: center;
      color: #a1a1aa;
      font-weight: 500;
      font-variant-numeric: tabular-nums;
    }
  `;

  const wrap = document.createElement("div");
  wrap.className = "wrap";

  const bar = document.createElement("div");
  bar.className = "bar";

  const statusEl = document.createElement("div");
  statusEl.className = "status";

  const podsBadge = document.createElement("span");
  podsBadge.className = "pods-badge";
  podsBadge.hidden = true;

  const podsBadgeDot = document.createElement("span");
  podsBadgeDot.className = "pods-badge-dot";
  podsBadgeDot.setAttribute("aria-hidden", "true");

  const podsBadgeCount = document.createElement("span");
  podsBadgeCount.className = "pods-badge-count";

  const podsBadgeSpend = document.createElement("span");
  podsBadgeSpend.className = "pods-badge-spend";

  podsBadge.append(podsBadgeDot, podsBadgeCount, podsBadgeSpend);

  const statusLeft = document.createElement("span");
  statusLeft.className = "status-wing status-left";

  const statusCenter = document.createElement("span");
  statusCenter.className = "status-center";

  const gpuLabel = document.createElement("span");
  gpuLabel.className = "gpu-name";
  gpuLabel.textContent = "RunPod";

  const spentAmount = document.createElement("span");
  spentAmount.className = "spent";
  spentAmount.hidden = true;

  statusCenter.append(gpuLabel, spentAmount);

  const statusRight = document.createElement("span");
  statusRight.className = "status-wing status-right";

  statusEl.append(podsBadge, statusLeft, statusCenter, statusRight);

  const actionsDrawer = document.createElement("div");
  actionsDrawer.className = "actions-drawer";

  const actionsInner = document.createElement("div");
  actionsInner.className = "actions-inner";

  const actionsRow = document.createElement("div");
  actionsRow.className = "actions-row";

  const backBtn = document.createElement("button");
  backBtn.type = "button";
  backBtn.className = "pill-btn";
  backBtn.setAttribute("aria-label", "Back to pod list");
  backBtn.innerHTML = `<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 8H3.5"/><path d="M7 4.5 3.5 8 7 11.5"/></svg><span>Back to pod list</span>`;

  const restartBtn = document.createElement("button");
  restartBtn.type = "button";
  restartBtn.className = "pill-btn";
  restartBtn.setAttribute("aria-label", "Restart ComfyUI");
  restartBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M3 21v-5h5"/></svg><span>Restart</span>`;

  const shutdownBtn = document.createElement("button");
  shutdownBtn.type = "button";
  shutdownBtn.className = "pill-btn pill-btn-danger";
  shutdownBtn.setAttribute("aria-label", "Shutdown pod");
  shutdownBtn.innerHTML = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/></svg><span>Shutdown</span>`;

  const confirmBox = document.createElement("div");
  confirmBox.className = "confirm-box";
  confirmBox.hidden = true;

  const confirmText = document.createElement("span");
  confirmText.className = "confirm-text";
  confirmText.textContent = "Terminate pod?";

  const confirmCancelBtn = document.createElement("button");
  confirmCancelBtn.type = "button";
  confirmCancelBtn.className = "pill-btn";
  confirmCancelBtn.textContent = "Cancel";

  const confirmOkBtn = document.createElement("button");
  confirmOkBtn.type = "button";
  confirmOkBtn.className = "pill-btn pill-btn-danger-solid";
  confirmOkBtn.textContent = "Shutdown";

  confirmBox.append(confirmText, confirmCancelBtn, confirmOkBtn);
  actionsRow.append(backBtn, restartBtn, shutdownBtn, confirmBox);
  actionsInner.append(actionsRow);
  actionsDrawer.append(actionsInner);

  bar.append(statusEl, actionsDrawer);
  wrap.append(bar);
  shadow.append(style, wrap);

  let activePodId = "";
  let hourlyPrice = 0;
  let startedAtMs = 0;
  let activePodsCount = 1;
  let activePodsHourly = 0;
  let lastPodsSummaryFetchMs = 0;

  const COLLAPSE_DELAY_MS = 2800;
  let collapseTimer = null;

  function keepExpanded() {
    if (collapseTimer !== null) {
      clearTimeout(collapseTimer);
      collapseTimer = null;
    }
    wrap.classList.add("is-expanded");
  }

  function scheduleCollapse() {
    if (collapseTimer !== null) clearTimeout(collapseTimer);
    collapseTimer = setTimeout(() => {
      collapseTimer = null;
      if (!wrap.classList.contains("is-confirming")) {
        wrap.classList.remove("is-expanded");
      }
    }, COLLAPSE_DELAY_MS);
  }

  function collapseNow() {
    if (collapseTimer !== null) {
      clearTimeout(collapseTimer);
      collapseTimer = null;
    }
    if (wrap.classList.contains("is-confirming")) {
      setConfirmMode(false);
    }
    wrap.classList.remove("is-expanded");
    try { shadow.activeElement?.blur?.(); } catch {}
  }

  wrap.addEventListener("mouseenter", keepExpanded);
  wrap.addEventListener("mouseleave", scheduleCollapse);
  wrap.addEventListener("focusin", keepExpanded);
  wrap.addEventListener("focusout", (e) => {
    if (!wrap.contains(e.relatedTarget)) scheduleCollapse();
  });
  statusEl.addEventListener("click", () => {
    if (wrap.classList.contains("is-expanded") || wrap.classList.contains("is-confirming")) {
      collapseNow();
    } else {
      keepExpanded();
    }
  });

  function setConfirmMode(active) {
    wrap.classList.toggle("is-confirming", active);
    backBtn.hidden = active;
    restartBtn.hidden = active;
    shutdownBtn.hidden = active;
    confirmBox.hidden = !active;
    if (active) keepExpanded();
  }

  function formatUptime(elapsedMs) {
    const totalMin = Math.floor(Math.max(0, elapsedMs) / 60000);
    if (totalMin < 1) return "<1 мин";
    if (totalMin < 60) return `${totalMin} мин`;
    const hrs = Math.floor(totalMin / 60);
    const mins = totalMin % 60;
    return mins > 0 ? `${hrs}ч ${mins}м` : `${hrs}ч`;
  }

  function updateSpentText() {
    if (!(startedAtMs > 0)) {
      spentAmount.hidden = true;
      statusLeft.textContent = "";
      statusRight.textContent = "";
      return;
    }
    const elapsedMs = Math.max(0, Date.now() - startedAtMs);
    const uptime = formatUptime(elapsedMs);
    if (hourlyPrice > 0) {
      const dollars = (elapsedMs / 3600000) * hourlyPrice;
      statusLeft.textContent = `${uptime}\u00A0·\u00A0`;
      spentAmount.textContent = `$${dollars.toFixed(2)}`;
      statusRight.textContent = `\u00A0·\u00A0$${hourlyPrice.toFixed(2)}/ч`;
    } else {
      statusLeft.textContent = "";
      spentAmount.textContent = uptime;
      statusRight.textContent = "";
    }
    spentAmount.hidden = false;
  }

  function updatePodsBadge() {
    if (activePodsCount > 1) {
      podsBadgeCount.textContent = `${activePodsCount} pods`;
      podsBadgeSpend.textContent =
        activePodsHourly > 0 ? `\u00A0·\u00A0$${activePodsHourly.toFixed(2)}/ч` : "";
      podsBadge.title =
        activePodsHourly > 0
          ? `На аккаунте запущено ${activePodsCount} подов (всего $${activePodsHourly.toFixed(2)}/ч)`
          : `На аккаунте запущено ${activePodsCount} подов`;
      podsBadge.hidden = false;
    } else {
      podsBadge.hidden = true;
      podsBadgeCount.textContent = "";
      podsBadgeSpend.textContent = "";
      podsBadge.title = "";
    }
  }

  function refreshPodsSummary() {
    lastPodsSummaryFetchMs = Date.now();
    return fetch("/__rp_bridge/pods_summary", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data || data.ok !== true) return;
        activePodsCount = Number(data.activePodsCount) || 0;
        activePodsHourly = Number(data.activePodsHourly) || 0;
        const singleHourly = Number(data.activePodHourly);
        if (Number.isFinite(singleHourly) && singleHourly > 0) {
          hourlyPrice = singleHourly;
          updateSpentText();
        }
        updatePodsBadge();
      })
      .catch(() => {});
  }

  async function disconnectAndReload() {
    backBtn.disabled = true;
    await fetch("/__rp_bridge/disconnect", { method: "POST" });
    window.location.reload();
  }

  backBtn.addEventListener("click", disconnectAndReload);

  function notifyPillError(summary, message) {
    try {
      const toast = window.comfyAPI?.app?.app?.extensionManager?.toast;
      if (typeof toast?.add === "function") {
        toast.add({
          severity: "error",
          summary,
          detail: message,
          life: 5000,
        });
        return;
      }
    } catch {}
    console.error("[RunPod-Desktop]", summary, message);
  }

  async function triggerComfyReboot() {
    const attempts = [
      ["/api/v2/manager/reboot", { method: "POST", headers: { "Content-Type": "application/json" } }],
      ["/api/manager/reboot", { method: "POST", headers: { "Content-Type": "application/json" } }],
      ["/api/manager/reboot", { method: "GET" }],
      ["/manager/reboot", { method: "GET" }],
    ];
    for (const [url, init] of attempts) {
      let res;
      try {
        res = await fetch(url, init);
      } catch {
        // Expected when ComfyUI exits mid-response to restart.
        return { ok: true };
      }
      if (res.status === 404 || res.status === 405) continue;
      if (res.status === 403) {
        return {
          ok: false,
          error: "Перезапуск заблокирован настройками безопасности ComfyUI-Manager (security_level).",
        };
      }
      if (res.status >= 400 && res.status < 500) {
        const msg = await res.text().catch(() => "");
        return { ok: false, error: msg || `Ошибка перезапуска ComfyUI (${res.status})` };
      }
      return { ok: true };
    }
    return { ok: false, error: "Эндпоинт перезапуска ComfyUI-Manager недоступен." };
  }

  function waitForComfyReconnect() {
    let settled = false;
    let pollTimer = null;
    let startDelayTimer = null;
    let maxTimeoutTimer = null;
    const api = window.comfyAPI?.api?.api;

    function finish() {
      if (settled) return;
      settled = true;
      if (startDelayTimer !== null) clearTimeout(startDelayTimer);
      if (pollTimer !== null) clearInterval(pollTimer);
      if (maxTimeoutTimer !== null) clearTimeout(maxTimeoutTimer);
      if (typeof api?.removeEventListener === "function") {
        try { api.removeEventListener("reconnected", finish); } catch {}
      }
      restartBtn.disabled = false;
      restartBtn.classList.remove("is-busy");
      fetch("/__rp_bridge/agent/refresh", { method: "POST" }).catch(() => {});
    }

    if (typeof api?.addEventListener === "function") {
      api.addEventListener("reconnected", finish, { once: true });
    }

    startDelayTimer = setTimeout(() => {
      if (settled) return;
      pollTimer = setInterval(async () => {
        if (settled) return;
        try {
          const r = await fetch("/api/system_stats", { cache: "no-store" });
          if (r.ok) finish();
        } catch {}
      }, 1500);
    }, 2000);

    maxTimeoutTimer = setTimeout(finish, 45000);
  }

  restartBtn.addEventListener("click", async () => {
    restartBtn.disabled = true;
    restartBtn.classList.add("is-busy");
    const result = await triggerComfyReboot();
    if (!result.ok) {
      notifyPillError("Restart ComfyUI", result.error);
      restartBtn.disabled = false;
      restartBtn.classList.remove("is-busy");
      return;
    }
    waitForComfyReconnect();
  });

  shutdownBtn.addEventListener("click", () => {
    setConfirmMode(true);
  });

  confirmCancelBtn.addEventListener("click", () => {
    setConfirmMode(false);
  });

  confirmOkBtn.addEventListener("click", async () => {
    confirmOkBtn.disabled = true;
    confirmCancelBtn.disabled = true;
    backBtn.disabled = true;
    confirmOkBtn.textContent = "Shutting down...";
    try {
      const res = await fetch("/__rp_bridge/pod/terminate", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `HTTP ${res.status}`);
      }
      window.location.reload();
    } catch (err) {
      notifyPillError("Shutdown Pod", err.message || "Не удалось удалить под.");
      confirmOkBtn.disabled = false;
      confirmCancelBtn.disabled = false;
      backBtn.disabled = false;
      confirmOkBtn.textContent = "Shutdown";
      setConfirmMode(false);
    }
  });

  // Prevent & clean up Comfy Desktop's local terminal stopgap ("Open Desktop Terminal")
  function cleanupDesktopTerminalStopgap() {
    const stopgap = window.__comfyDesktopTerminalStopgap;
    if (stopgap) {
      stopgap.registered = true;
      stopgap.tabBarInjectorStarted = true;
      try { stopgap.tabBarObserver?.disconnect(); } catch {}
    }

    document.querySelectorAll(".__comfy-desktop-popout-btn").forEach((el) => el.remove());

    try {
      const app = window.comfyAPI?.app?.app;
      const bottomPanel = app?.extensionManager?.bottomPanel;
      const tabs = bottomPanel?.panels?.terminal?.tabs || bottomPanel?.bottomPanelTabs;
      if (Array.isArray(tabs)) {
        const stopgapIdx = tabs.findIndex((t) => {
          if (!t || t.id !== "command-terminal") return false;
          const src = String(t.render || "");
          return (
            src.includes("protected Desktop window") ||
            src.includes("openDesktopTerminal") ||
            src.includes("__comfyDesktopTerminalStopgap")
          );
        });
        if (stopgapIdx !== -1) {
          const fallbackTab = tabs.find((t, idx) => idx !== stopgapIdx && t?.id);
          if (typeof bottomPanel.unregisterTab === "function") {
            bottomPanel.unregisterTab("command-terminal");
          } else {
            tabs.splice(stopgapIdx, 1);
          }
          if (bottomPanel.activeBottomPanelTabId === "command-terminal") {
            bottomPanel.activeBottomPanelTabId = fallbackTab ? fallbackTab.id : "logs-terminal";
          }
        }
      }
    } catch {}

    // Remove any already-rendered stopgap block inside the bottom panel body
    const candidates = document.querySelectorAll("div, span, p");
    for (const el of candidates) {
      if (host.contains(el)) continue;
      if (el.childElementCount !== 0) continue;
      if ((el.textContent || "").trim() !== "Terminal commands run in a protected Desktop window.") continue;
      const contentBox = el.parentElement;
      const container = contentBox?.parentElement;
      if (contentBox) contentBox.remove();
      if (container) {
        if (container.children.length === 0) {
          container.style.setProperty("display", "none", "important");
        } else {
          container.style.removeProperty("display");
          container.style.removeProperty("align-items");
          container.style.removeProperty("justify-content");
        }
      }
    }
  }

  let terminalScanQueued = false;
  function scheduleTerminalScan() {
    if (terminalScanQueued) return;
    terminalScanQueued = true;
    requestAnimationFrame(() => {
      terminalScanQueued = false;
      cleanupDesktopTerminalStopgap();
    });
  }

  function startTerminalObserver() {
    const root = document.body || document.documentElement;
    if (!root) return;
    cleanupDesktopTerminalStopgap();
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.target === host || host.contains(m.target)) continue;
        if (m.type === "childList" && (m.addedNodes.length > 0 || m.removedNodes.length > 0)) {
          scheduleTerminalScan();
          return;
        }
      }
    });
    observer.observe(root, { childList: true, subtree: true });

    let tries = 0;
    const timer = setInterval(() => {
      cleanupDesktopTerminalStopgap();
      tries += 1;
      if (tries >= 24) clearInterval(timer);
    }, 250);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startTerminalObserver, { once: true });
  } else {
    startTerminalObserver();
  }

  setInterval(updateSpentText, 1000);
  setInterval(() => {
    if (activePodId && document.visibilityState !== "hidden") {
      refreshPodsSummary();
    }
  }, 120000);
  document.addEventListener("visibilitychange", () => {
    if (
      activePodId &&
      document.visibilityState !== "hidden" &&
      Date.now() - lastPodsSummaryFetchMs >= 120000
    ) {
      refreshPodsSummary();
    }
  });

  fetch("/__rp_bridge/status", { cache: "no-store" })
    .then((r) => r.json())
    .then((data) => {
      if (typeof data?.autoDownloadOutputs === "boolean") {
        autoDownloadEnabled = data.autoDownloadOutputs;
      }
      const pod = data?.activePod;
      if (!pod) return;
      activePodId = pod.podId || "";
      hourlyPrice = Number(pod.price) || 0;
      startedAtMs = Number(pod.startedAtMs) || Date.now();
      activePodsCount = Number(pod.activePodsCount) || 1;
      activePodsHourly = Number(pod.activePodsHourly) || 0;
      gpuLabel.textContent = pod.gpuName || "RunPod";
      updateSpentText();
      updatePodsBadge();
      refreshPodsSummary();
    })
    .catch(() => {});
})();
