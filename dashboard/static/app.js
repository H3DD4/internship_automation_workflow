/* Dashboard behavior: live refresh, selection + sending, row actions,
   credential validation. Kept out of the template so it's readable and
   cacheable. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const val = (id) => ($(id) || {}).value || "";

  // Every state-changing request carries the session's CSRF token; a
  // session that ended (401) sends the user back to sign in.
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    });
    if (response.status === 401) {
      window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
      return { ok: false, message: "Your session ended — sign in again." };
    }
    return response.json();
  }

  // ── Confirm modal ────────────────────────────────────────────────────────
  // Replaces window.confirm, which couldn't show WHICH companies were about
  // to be emailed — the one thing worth checking before an irreversible send.
  const modal = {
    el: $("confirm-modal"),
    open(recipients, capRemaining) {
      if (!this.el) return Promise.resolve(true);
      const list = $("confirm-list");
      const warn = $("confirm-warn");
      $("confirm-copy").textContent =
        `${recipients.length} email${recipients.length === 1 ? "" : "s"} will be sent from your Gmail, ` +
        `paced a few minutes apart. This can't be undone.`;
      list.innerHTML = "";
      recipients.slice(0, 50).forEach((r) => {
        const li = document.createElement("li");
        li.textContent = `${r.company} · ${r.email}`;
        list.appendChild(li);
      });
      if (recipients.length > 50) {
        const li = document.createElement("li");
        li.textContent = `…and ${recipients.length - 50} more`;
        list.appendChild(li);
      }
      if (typeof capRemaining === "number" && recipients.length > capRemaining) {
        warn.hidden = false;
        warn.textContent = `Only ${capRemaining} send(s) left in today's cap — the rest stay ready and resume tomorrow.`;
      } else {
        warn.hidden = true;
      }
      this.el.hidden = false;
      $("confirm-ok").focus();

      return new Promise((resolve) => {
        const done = (result) => {
          this.el.hidden = true;
          $("confirm-ok").onclick = null;
          $("confirm-cancel").onclick = null;
          document.removeEventListener("keydown", onKey);
          resolve(result);
        };
        const onKey = (e) => {
          if (e.key === "Escape") done(false);
          // Enter means "yes" only on the Send button itself — on Cancel it
          // must cancel, never send.
          if (e.key === "Enter") { e.preventDefault(); done(document.activeElement === $("confirm-ok")); }
        };
        $("confirm-ok").onclick = () => done(true);
        $("confirm-cancel").onclick = () => done(false);
        this.el.onclick = (e) => { if (e.target === this.el) done(false); };
        document.addEventListener("keydown", onKey);
      });
    },
  };

  // ── Live state shared across features ────────────────────────────────────
  // Seeded from the server-rendered page so the cap warning and the duration
  // estimate are correct on first load, not only after the first refresh tick.
  const funnelEl = $("funnel");
  const seededCap = funnelEl ? parseInt(funnelEl.dataset.capRemaining, 10) : NaN;
  const seededDelay = funnelEl ? parseInt(funnelEl.dataset.avgDelay, 10) : NaN;
  const state = {
    sending: false,       // a send job is polling
    capRemaining: Number.isNaN(seededCap) ? null : seededCap,
    avgDelay: Number.isNaN(seededDelay) || seededDelay <= 0 ? 80 : seededDelay,
  };

  // ── Selection + batch bar ────────────────────────────────────────────────
  const batchBar = $("batch-actions");
  const countEl = $("selected-count");
  const estimateEl = $("selected-estimate");
  const selectAllCb = $("select-all-cb");

  const checkboxes = () => Array.from(document.querySelectorAll(".row-select-cb"));
  const checkedIds = () => checkboxes().filter((c) => c.checked).map((c) => +c.dataset.appId);

  // Only companies with a finished draft can be sent; any selected company
  // can be re-scanned.
  function selectedRecipients() {
    return checkboxes().filter((c) => c.checked && c.dataset.sendable === "1").map((c) => {
      const row = c.closest("tr");
      return { id: +c.dataset.appId, company: row.dataset.company, email: row.dataset.email };
    });
  }

  function updateBatchBar() {
    const ids = checkedIds();
    if (!batchBar) return;
    batchBar.hidden = ids.length === 0;
    const sendable = selectedRecipients().length;
    if (countEl) {
      countEl.textContent = sendable === ids.length
        ? `${ids.length} selected`
        : `${ids.length} selected · ${sendable} ready to send`;
    }
    const sendBtn = $("send-selected-btn");
    if (sendBtn && !state.sending) sendBtn.disabled = sendable === 0;
    if (estimateEl && sendable) {
      const mins = Math.max(1, Math.round((sendable * state.avgDelay) / 60));
      const capNote = (state.capRemaining !== null && sendable > state.capRemaining)
        ? ` · only ${state.capRemaining} left in today's cap`
        : "";
      estimateEl.textContent = `≈ ${mins} min at current pacing${capNote}`;
    } else if (estimateEl) {
      estimateEl.textContent = "";
    }
    if (selectAllCb) {
      const all = checkboxes();
      selectAllCb.checked = all.length > 0 && ids.length === all.length;
    }
  }

  if (selectAllCb) {
    selectAllCb.addEventListener("change", () => {
      checkboxes().forEach((c) => { c.checked = selectAllCb.checked; });
      updateBatchBar();
    });
  }
  document.addEventListener("change", (e) => {
    if (e.target.classList && e.target.classList.contains("row-select-cb")) updateBatchBar();
  });
  const deselectBtn = $("deselect-all-btn");
  if (deselectBtn) {
    deselectBtn.addEventListener("click", () => {
      checkboxes().forEach((c) => { c.checked = false; });
      if (selectAllCb) selectAllCb.checked = false;
      updateBatchBar();
    });
  }

  // ── Send toast + job polling ─────────────────────────────────────────────
  const toast = $("send-toast");
  function showToast(text, pct) {
    if (!toast) return;
    toast.hidden = false;
    $("send-toast-text").textContent = text;
    $("send-toast-fill").style.width = `${Math.round((pct || 0) * 100)}%`;
  }
  function hideToast() { if (toast) toast.hidden = true; }

  async function createSendJob(appIds) {
    state.sending = true;
    showToast("Creating send job…", 0);
    try {
      const data = await postJSON("/api/send-job", { app_ids: appIds });
      if (!data.ok) {
        showToast(`✗ ${data.message}`, 0);
        setTimeout(hideToast, 4000);
        state.sending = false;
        return;
      }
      pollJob(data.job_id, data.total);
    } catch (err) {
      showToast(`✗ Network error: ${err.message}`, 0);
      setTimeout(hideToast, 4000);
      state.sending = false;
    }
  }

  async function pollJob(jobId, total) {
    let data;
    try {
      const response = await fetch(`/api/send-job/${jobId}`);
      data = await response.json();
    } catch (err) {
      setTimeout(() => pollJob(jobId, total), 3000);
      return;
    }
    if (!data.ok) { state.sending = false; return; }

    const done = data.sent + data.failed;
    showToast(
      `Sending: ${data.sent} sent, ${data.failed} failed, ${data.queued + data.sending} to go`,
      total > 0 ? done / total : 0
    );

    (data.items || []).forEach((item) => {
      const row = document.querySelector(`tr[data-app-id="${item.app_id}"]`);
      if (!row) return;
      const pill = row.querySelector(".status-pill");
      if (pill) {
        pill.className = `status-pill status-${item.status}`;
        const labels = { sent: "Sent", failed: "Failed", sending: "Sending", queued: "Queued", retry_wait: "Retry later" };
        pill.textContent = labels[item.status] || item.status;
      }
      if (item.status === "sent") {
        const cb = row.querySelector(".row-select-cb");
        if (cb) { cb.checked = false; cb.disabled = true; }
        const btn = row.querySelector(".send-one-btn");
        if (btn) btn.remove();
      }
    });
    updateBatchBar();

    if (data.status === "completed") {
      const failedNote = data.failed ? ` — ${data.failed} failed, see the Problems tab` : "";
      showToast(`✓ Done: ${data.sent} sent${failedNote}.`, 1);
      state.sending = false;
      setTimeout(hideToast, data.failed ? 8000 : 4000);
      refresh();
      return;
    }
    setTimeout(() => pollJob(jobId, total), 2000);
  }

  const sendSelectedBtn = $("send-selected-btn");
  if (sendSelectedBtn) {
    sendSelectedBtn.addEventListener("click", async () => {
      const recipients = selectedRecipients();
      if (!recipients.length) return;
      if (!(await modal.open(recipients, state.capRemaining))) return;
      createSendJob(recipients.map((r) => r.id));
    });
  }

  // ── Favorites ────────────────────────────────────────────────────────────
  const onFavoritesTab = new URLSearchParams(window.location.search).get("status") === "favorites";

  function renderFavoriteCounts(total, sendable) {
    const tabCount = document.querySelector("[data-favorites-count]");
    if (tabCount) { tabCount.textContent = total; tabCount.hidden = !total; }
    const totalEl = document.querySelector("[data-favorites-total]");
    if (totalEl) totalEl.textContent = total;
    const sendableEl = document.querySelector("[data-favorites-sendable]");
    if (sendableEl) sendableEl.textContent = sendable;
    const sendFavBtn = $("send-favorites-btn");
    if (sendFavBtn && !state.sending) sendFavBtn.disabled = !sendable;
  }

  function paintStar(btn, on) {
    const company = btn.closest("tr") ? btn.closest("tr").dataset.company : "";
    btn.classList.toggle("fav-btn--on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.textContent = on ? "★" : "☆";
    btn.title = on ? "Remove from favorites" : "Add to favorites";
    btn.setAttribute("aria-label", `${on ? "Remove" : "Add"} ${company} ${on ? "from" : "to"} favorites`);
  }

  async function setFavorite(ids, on) {
    const data = await postJSON("/api/favorite", { app_ids: ids, favorite: on });
    if (!data.ok) throw new Error(data.message);
    renderFavoriteCounts(data.favorites_total, data.favorites_sendable);
    return data;
  }

  // ── Re-scan: research and write the selected companies again ─────────────
  const rescanBtn = $("rescan-selected-btn");
  if (rescanBtn) {
    rescanBtn.addEventListener("click", async () => {
      const ids = checkedIds();
      if (!ids.length) return;
      const n = ids.length;
      if (!window.confirm(`Re-scan ${n} compan${n === 1 ? "y" : "ies"}?\n\n` +
          "Their research and drafts are cleared and done again from scratch with your AI key. " +
          "Any edits you made to these drafts are lost. Sent and skipped companies are left alone.")) return;
      rescanBtn.disabled = true;
      try {
        const data = await postJSON("/api/rescan", { app_ids: ids });
        showToast(`${data.ok ? "↻" : "✗"} ${data.message}`, data.ok ? 1 : 0);
        setTimeout(hideToast, 4500);
        if (data.ok) {
          checkboxes().forEach((c) => { c.checked = false; });
          updateBatchBar();
          refresh();
        }
      } catch (err) {
        showToast(`✗ ${err.message}`, 0);
        setTimeout(hideToast, 4000);
      } finally {
        rescanBtn.disabled = false;
      }
    });
  }

  const favoriteSelectedBtn = $("favorite-selected-btn");
  if (favoriteSelectedBtn) {
    favoriteSelectedBtn.addEventListener("click", async () => {
      const ids = checkedIds();
      if (!ids.length) return;
      favoriteSelectedBtn.disabled = true;
      try {
        await setFavorite(ids, true);
        ids.forEach((id) => {
          const star = document.querySelector(`.fav-btn[data-app-id="${id}"]`);
          if (star) paintStar(star, true);
        });
        showToast(`★ ${ids.length} added to favorites`, 1);
        setTimeout(hideToast, 2500);
      } catch (err) {
        showToast(`✗ ${err.message}`, 0);
        setTimeout(hideToast, 4000);
      } finally {
        favoriteSelectedBtn.disabled = false;
      }
    });
  }

  const sendFavoritesBtn = $("send-favorites-btn");
  if (sendFavoritesBtn) {
    sendFavoritesBtn.addEventListener("click", async () => {
      sendFavoritesBtn.disabled = true;
      try {
        const response = await fetch("/api/favorites/sendable");
        const data = await response.json();
        const recipients = data.recipients || [];
        if (!recipients.length) return;
        // The same confirm dialog as every other send: lists who gets what
        // and warns when today's cap is smaller than the batch.
        if (!(await modal.open(recipients, state.capRemaining))) return;
        createSendJob(recipients.map((r) => r.id));
      } finally {
        sendFavoritesBtn.disabled = false;
      }
    });
  }

  // ── Per-row actions (delegated, so live-refreshed rows keep working) ─────
  document.addEventListener("click", async (e) => {
    const favBtn = e.target.closest(".fav-btn");
    if (favBtn) {
      const on = !favBtn.classList.contains("fav-btn--on");
      paintStar(favBtn, on);          // instant feedback; undone below on failure
      try {
        await setFavorite([+favBtn.dataset.appId], on);
        if (onFavoritesTab && !on) refresh();   // it no longer belongs in this view
      } catch (err) {
        paintStar(favBtn, !on);
      }
      return;
    }

    const sendBtn = e.target.closest(".send-one-btn");
    if (sendBtn) {
      const row = sendBtn.closest("tr");
      const recipient = { id: +sendBtn.dataset.appId, company: row.dataset.company, email: row.dataset.email };
      if (!(await modal.open([recipient], state.capRemaining))) return;
      sendBtn.disabled = true;
      sendBtn.textContent = "Sending…";
      createSendJob([recipient.id]);
      return;
    }

    const skipBtn = e.target.closest(".skip-btn");
    const unskipBtn = e.target.closest(".unskip-btn");
    if (skipBtn || unskipBtn) {
      const btn = skipBtn || unskipBtn;
      btn.disabled = true;
      const data = await postJSON(`/api/skip/${btn.dataset.appId}`, { unskip: !!unskipBtn });
      if (!data.ok) { btn.disabled = false; alert(data.message); return; }
      refresh();
    }
  });

  // ── Live refresh, in place ───────────────────────────────────────────────
  const refreshToggle = $("auto-refresh-toggle");
  const REFRESH_MS = 5000;
  let refreshTimer = null;
  let setupDirty = false;
  const setupForm = document.querySelector(".setup-form");
  if (setupForm) {
    setupForm.addEventListener("input", () => { setupDirty = true; });
    setupForm.addEventListener("submit", () => { setupDirty = false; });
  }

  if (refreshToggle) {
    const stored = localStorage.getItem("dashboardAutoRefresh");
    if (stored !== null) refreshToggle.checked = stored === "true";
    refreshToggle.addEventListener("change", () => {
      localStorage.setItem("dashboardAutoRefresh", refreshToggle.checked);
      schedule();
    });
  }

  function applyOverview(data) {
    // Rows: replace the tbody only when the markup actually changed, so a
    // refresh never steals focus or flickers, and re-check whatever the user
    // had selected before.
    const tbody = $("app-tbody");
    if (tbody && typeof data.rows_html === "string") {
      const selected = new Set(checkedIds());
      const next = data.rows_html.trim();
      if (tbody.dataset.signature !== next) {
        tbody.innerHTML = next;
        tbody.dataset.signature = next;
        checkboxes().forEach((c) => {
          if (selected.has(+c.dataset.appId)) c.checked = true;
        });
      }
      const empty = $("empty-state");
      if (empty) empty.hidden = data.table_total > 0;
    }

    // Funnel values + today's cap meter
    (data.funnel || []).forEach((card) => {
      const el = document.querySelector(`[data-funnel-key="${card.key}"] [data-funnel-value]`);
      if (el) el.textContent = card.value;
    });
    const capText = document.querySelector("[data-cap-text]");
    if (capText) capText.textContent = `${data.sent_today} / ${data.max_per_day} today`;
    const capFill = document.querySelector("[data-cap-fill]");
    if (capFill && data.max_per_day) {
      capFill.style.width = `${Math.min(100, (100 * data.sent_today) / data.max_per_day)}%`;
    }
    state.capRemaining = data.cap_remaining;

    // Favorites counts (a send or new drafts change how many are ready)
    if (typeof data.favorites_total === "number") {
      renderFavoriteCounts(data.favorites_total, data.favorites_sendable);
    }

    // Companies with no CV match: their tab's count and the "hidden" note
    if (typeof data.no_match_count === "number") {
      const n = data.no_match_count;
      const tab = document.querySelector("[data-no-match-count]");
      if (tab) { tab.textContent = n; tab.hidden = !n; }
      const total = document.querySelector("[data-no-match-total]");
      if (total) total.textContent = n;
      const note = document.querySelector("[data-no-match-note]");
      if (note) note.hidden = !n;
    }

    // Undelivered emails: red alert + Problems card note
    renderUndelivered(data.bounced_count || 0);
    if (data.bounce_check) renderBounceCheck(data.bounce_check);

    // Preparation progress
    const prepared = data.total_count - data.pending_prep;
    const prepText = document.querySelector("[data-prep-progress]");
    if (prepText) prepText.textContent = `${prepared} / ${data.total_count}`;
    const prepFill = document.querySelector("[data-prep-fill]");
    if (prepFill && data.total_count) {
      prepFill.style.width = `${(100 * prepared) / data.total_count}%`;
    }

    // Table meta + activity indicator
    const meta = document.querySelector("[data-table-meta]");
    if (meta) meta.textContent = `${data.table_total} row(s) · page ${data.table_page}/${data.table_pages}`;
    const liveText = $("live-text");
    const liveDot = $("live-dot");
    const liveNote = $("live-note");
    if (liveText) liveText.textContent = data.in_progress_count ? `${data.in_progress_count} in progress` : "idle";
    if (liveDot) liveDot.classList.toggle("dot--pulse", data.in_progress_count > 0);
    if (liveNote) liveNote.classList.toggle("live-note--active", data.in_progress_count > 0);

    // Run controls: reflect the real state of the preparation process, so a
    // Stop shows "Stopping…" at once and "Start preparation" when it's done.
    renderRunState(data.running, data.stop_requested);

    // Activity log, keeping it pinned to the bottom while it's already there
    const log = $("log-output");
    if (log && data.log_tail && log.textContent !== data.log_tail) {
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.textContent = data.log_tail;
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
  }

  function renderRunState(running, stopRequested) {
    const start = $("start-prep-btn");
    const stop = $("stop-prep-btn");
    const note = $("run-state-note");
    if (start) {
      start.disabled = running || !start.dataset.prepReady;
      start.textContent = running ? "Preparing…" : "Start preparation";
    }
    if (stop) {
      stop.hidden = !running;
      stop.disabled = !!stopRequested;
      stop.textContent = stopRequested ? "Stopping…" : "Stop safely";
    }
    if (note) {
      note.textContent = running && stopRequested
        ? "Finishing the companies already in progress, then stopping." : "";
    }
  }

  function renderUndelivered(count) {
    const alert = $("delivery-alert");
    if (alert) {
      alert.hidden = count === 0;
      const n = alert.querySelector("[data-undelivered-count]");
      if (n) n.textContent = count;
      const plural = alert.querySelector("[data-undelivered-plural]");
      if (plural) plural.textContent = count === 1 ? "" : "s";
    }
    const note = document.querySelector("[data-undelivered-text]");
    if (note) {
      note.hidden = count === 0;
      note.textContent = `${count} not delivered`;
    }
  }

  function timeAgo(iso) {
    const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (!isFinite(minutes)) return "";
    if (minutes < 1) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
  }

  function renderBounceCheck(check) {
    const el = $("bounce-last-check");
    if (!el) return;
    el.textContent = "";
    if (check.last_error) {
      const span = document.createElement("span");
      span.className = "text-danger";
      span.textContent = `Last check failed: ${check.last_error}`;
      el.appendChild(span);
    } else if (check.last_at) {
      el.textContent = `Last checked ${timeAgo(check.last_at)}`
        + (check.last_trigger === "auto" ? " (automatic)" : "");
    } else {
      el.textContent = "Not checked yet.";
    }
  }
  const lastCheckEl = $("bounce-last-check");
  if (lastCheckEl && lastCheckEl.dataset.lastAt && !lastCheckEl.querySelector(".text-danger")) {
    renderBounceCheck({ last_at: lastCheckEl.dataset.lastAt });
  }

  async function refresh() {
    const params = new URLSearchParams(window.location.search);
    try {
      const response = await fetch(`/api/overview?${params.toString()}`);
      if (response.status === 401) {
        window.location.href = "/login";
        return;
      }
      const data = await response.json();
      if (data.ok) applyOverview(data);
    } catch (err) {
      /* keep the last known state and try again on the next tick */
    }
  }

  function schedule() {
    if (refreshTimer) clearTimeout(refreshTimer);
    if (!refreshToggle || !refreshToggle.checked) return;
    refreshTimer = setTimeout(async () => {
      const active = document.activeElement;
      const typing = active && (active.closest(".table-controls") || (setupForm && setupForm.contains(active)));
      const modalOpen = modal.el && !modal.el.hidden;
      // Never refresh over unsaved setup input, mid-typing, or while a
      // confirm dialog is waiting on the user.
      if (!typing && !setupDirty && !modalOpen && document.visibilityState !== "hidden") {
        await refresh();
      }
      schedule();
    }, REFRESH_MS);
  }
  schedule();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });

  // ── Credential validation (no save, no reload, no lost typing) ───────────
  const gmailFields = ["gmail-address", "gmail-password"];
  const aiFields = ["ai-key", "ai-base-url", "ai-model", "ai-provider"];

  function markInvalid(fieldIds) {
    fieldIds.forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.classList.remove("shake");
      void el.offsetWidth; // force reflow so the animation replays
      el.classList.add("input-error", "shake");
      el.addEventListener("animationend", () => el.classList.remove("shake"), { once: true });
    });
  }
  function clearInvalid(fieldIds) {
    fieldIds.forEach((id) => {
      const el = $(id);
      if (el) el.classList.remove("input-error", "shake");
    });
  }

  function wireValidate(buttonId, resultId, endpoint, buildPayload, fieldIds) {
    const button = $(buttonId);
    const result = $(resultId);
    if (!button || !result) return;
    button.addEventListener("click", async () => {
      result.textContent = "Checking…";
      result.classList.remove("validation-result--ok", "validation-result--bad");
      clearInvalid(fieldIds);
      button.disabled = true;
      try {
        const data = await postJSON(endpoint, buildPayload());
        result.textContent = (data.ok ? "✓ " : "✗ ") + data.message;
        result.classList.add(data.ok ? "validation-result--ok" : "validation-result--bad");
        if (data.ok) clearInvalid(fieldIds); else markInvalid(fieldIds);
      } catch (err) {
        result.textContent = "✗ Could not reach the dashboard server — is it still running?";
        result.classList.add("validation-result--bad");
        markInvalid(fieldIds);
      } finally {
        button.disabled = false;
      }
    });
    // A result only describes what was typed at click time — clear it on edit.
    fieldIds.forEach((id) => {
      const field = $(id);
      if (field) field.addEventListener("input", () => {
        result.textContent = "";
        result.classList.remove("validation-result--ok", "validation-result--bad");
        clearInvalid(fieldIds);
      });
    });
  }

  wireValidate("validate-gmail-btn", "gmail-validation-result", "/api/validate-gmail", () => ({
    mail_method: (document.querySelector('input[name="mail_method"]:checked') || {}).value || "",
    gmail_address: val("gmail-address").trim(),
    gmail_app_password: val("gmail-password").trim(),
    smtp_host: val("smtp-host").trim(),
    smtp_port: val("smtp-port").trim(),
    smtp_security: val("smtp-security").trim(),
    smtp_username: val("smtp-username").trim(),
  }), gmailFields);

  const aiPayload = () => ({
    ai_provider: val("ai-provider").trim(),
    ai_api_key: val("ai-key").trim(),
    ai_base_url: val("ai-base-url").trim(),
    ai_model: val("ai-model").trim(),
  });
  wireValidate("validate-ai-btn", "ai-validation-result", "/api/validate-ai", aiPayload, aiFields);

  // ── Provider picker: each provider has its own base URL, key and models ──
  const providerSelect = $("ai-provider");
  const modelInput = $("ai-model");
  const modelOptions = $("ai-model-options");
  const modelsResult = $("models-result");

  function fillModelOptions(models) {
    if (!modelOptions) return;
    modelOptions.innerHTML = "";
    models.forEach((m) => {
      const option = document.createElement("option");
      option.value = m;
      modelOptions.appendChild(option);
    });
  }

  if (providerSelect) {
    providerSelect.addEventListener("change", () => {
      const opt = providerSelect.selectedOptions[0];
      const baseUrl = $("ai-base-url");
      if (baseUrl) baseUrl.value = opt.dataset.baseUrl || "";
      const key = $("ai-key");
      if (key) {
        key.value = "";
        key.placeholder = opt.dataset.keySaved ? "Saved — enter to replace" : "Paste your key";
      }
      const hint = $("ai-portal-hint");
      if (hint) {
        hint.innerHTML = "";
        if (opt.dataset.portal) {
          const a = document.createElement("a");
          a.href = opt.dataset.portal; a.target = "_blank"; a.rel = "noreferrer";
          a.textContent = "Get a key";
          hint.appendChild(a);
          hint.appendChild(document.createTextNode(". "));
        }
        hint.appendChild(document.createTextNode("Stored only in your local .env."));
      }
      const suggested = (opt.dataset.suggested || "").split(",").filter(Boolean);
      fillModelOptions(suggested);
      if (modelInput) modelInput.value = opt.dataset.defaultModel || "";
      if (modelsResult) modelsResult.textContent = "";
    });
  }

  const loadModelsBtn = $("load-models-btn");
  if (loadModelsBtn) {
    loadModelsBtn.addEventListener("click", async () => {
      loadModelsBtn.disabled = true;
      modelsResult.textContent = "Loading…";
      modelsResult.classList.remove("validation-result--ok", "validation-result--bad");
      try {
        const data = await postJSON("/api/models", aiPayload());
        modelsResult.textContent = (data.ok ? "✓ " : "✗ ") + data.message +
          (data.ok ? " Click the Model field to pick one." : "");
        modelsResult.classList.add(data.ok ? "validation-result--ok" : "validation-result--bad");
        if (data.ok) {
          fillModelOptions(data.models);
          if (modelInput && !modelInput.value && data.models.length) modelInput.value = data.models[0];
          if (modelInput) modelInput.focus();
        }
      } catch (err) {
        modelsResult.textContent = "✗ Could not reach the dashboard server.";
        modelsResult.classList.add("validation-result--bad");
      } finally {
        loadModelsBtn.disabled = false;
      }
    });
  }

  // ── Model pool: live check of every model ────────────────────────────────
  const poolBtn = $("check-pool-btn");
  if (poolBtn) {
    const poolResult = $("pool-result");
    const LABELS = {
      ok: "Working", busy: "Busy (rate-limited)", inactive: "Plan not active",
      bad_key: "Key rejected", unavailable: "Not available", error: "Error",
    };
    poolBtn.addEventListener("click", async () => {
      poolBtn.disabled = true;
      poolBtn.textContent = "Checking…";
      poolResult.textContent = "";
      poolResult.classList.remove("validation-result--ok", "validation-result--bad");
      try {
        const data = await postJSON("/api/ai-health", {});
        (data.rows || []).forEach((row) => {
          const tr = document.querySelector(`[data-pool-name="${CSS.escape(row.name)}"]`);
          if (!tr) return;
          const cell = tr.querySelector(".pool-state");
          cell.textContent = "";
          const pill = document.createElement("span");
          pill.className = `pool-pill pool-pill--${row.state}`;
          pill.textContent = LABELS[row.state] || row.state;
          const detail = document.createElement("span");
          detail.className = "field-hint pool-detail";
          detail.textContent = row.detail || "";
          cell.append(pill, detail);
        });
        poolResult.textContent = (data.ok ? "✓ " : "✗ ") + data.message;
        poolResult.classList.add(data.ok ? "validation-result--ok" : "validation-result--bad");
      } catch (err) {
        poolResult.textContent = "✗ Could not reach the dashboard server.";
        poolResult.classList.add("validation-result--bad");
      } finally {
        poolBtn.disabled = false;
        poolBtn.textContent = "Check all models";
      }
    });
  }

  // ── Bounce check ─────────────────────────────────────────────────────────
  const bounceBtn = $("check-bounces-btn");
  if (bounceBtn) {
    const result = $("check-bounces-result");
    bounceBtn.addEventListener("click", async () => {
      bounceBtn.disabled = true;
      bounceBtn.textContent = "Checking…";
      result.textContent = "";
      try {
        const data = await postJSON("/api/check-bounces", {});
        result.textContent = (data.ok ? "" : "✗ ") + data.message;
        if (data.bounce_check) renderBounceCheck(data.bounce_check);
        if (data.ok && data.updated) refresh();
      } catch (err) {
        result.textContent = `✗ Network error: ${err.message}`;
      } finally {
        bounceBtn.disabled = false;
        bounceBtn.textContent = "Check bounces now";
      }
    });
  }

  // ── Flash messages fade out on their own ─────────────────────────────────
  const flashStack = $("flash-stack");
  if (flashStack) {
    setTimeout(() => {
      flashStack.style.transition = "opacity .5s";
      flashStack.style.opacity = "0";
      setTimeout(() => flashStack.remove(), 600);
    }, 9000);
  }

  updateBatchBar();
})();
