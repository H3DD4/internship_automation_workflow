/* Dashboard behavior: live refresh, selection + sending, row actions,
   credential validation. Kept out of the template so it's readable and
   cacheable. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const val = (id) => ($(id) || {}).value || "";

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
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
          if (e.key === "Enter") done(true);
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

  function selectedRecipients() {
    return checkboxes().filter((c) => c.checked).map((c) => {
      const row = c.closest("tr");
      return { id: +c.dataset.appId, company: row.dataset.company, email: row.dataset.email };
    });
  }

  function updateBatchBar() {
    const ids = checkedIds();
    if (!batchBar) return;
    batchBar.hidden = ids.length === 0;
    if (countEl) countEl.textContent = `${ids.length} selected`;
    if (estimateEl && ids.length) {
      const mins = Math.max(1, Math.round((ids.length * state.avgDelay) / 60));
      const capNote = (state.capRemaining !== null && ids.length > state.capRemaining)
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

  // ── Per-row actions (delegated, so live-refreshed rows keep working) ─────
  document.addEventListener("click", async (e) => {
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

    // Activity log, keeping it pinned to the bottom while it's already there
    const log = $("log-output");
    if (log && data.log_tail && log.textContent !== data.log_tail) {
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.textContent = data.log_tail;
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
  }

  async function refresh() {
    const params = new URLSearchParams(window.location.search);
    try {
      const response = await fetch(`/api/overview?${params.toString()}`);
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
  const aiFields = ["ai-key", "ai-base-url", "ai-model"];

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
    gmail_address: val("gmail-address").trim(),
    gmail_app_password: val("gmail-password").trim(),
  }), gmailFields);

  wireValidate("validate-ai-btn", "ai-validation-result", "/api/validate-ai", () => ({
    ai_api_key: val("ai-key").trim(),
    ai_base_url: val("ai-base-url").trim(),
    ai_model: val("ai-model").trim(),
  }), aiFields);

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
