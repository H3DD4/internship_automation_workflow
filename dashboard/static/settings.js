/* Settings page: email-method switching and the companies import
   (check the file first, then import — nothing is saved on "Check"). */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  async function post(url, body, isForm) {
    const response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: isForm ? { "X-CSRF-Token": csrfToken() }
                      : { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: isForm ? body : JSON.stringify(body || {}),
    });
    if (response.status === 401) {
      window.location.href = "/login";
      return { ok: false, message: "Session ended." };
    }
    try { return await response.json(); } catch (_) { return { ok: false, message: `Server error (${response.status}).` }; }
  }

  const el = (tag, attrs, text) => {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // ── AI models: one card per provider ─────────────────────────────────────
  // Connect a key (checked against the provider before it's saved), then
  // tick models from the provider's live list — nothing is typed by hand.
  async function getJSON(url) {
    const response = await fetch(url, { credentials: "same-origin" });
    if (response.status === 401) { window.location.href = "/login"; return { ok: false, models: [] }; }
    try { return await response.json(); } catch (_) { return { ok: false, models: [], message: `Server error (${response.status}).` }; }
  }

  function setResult(card, text, ok) {
    const node = card.querySelector("[data-result]");
    node.textContent = text || "";
    node.classList.toggle("validation-result--ok", ok === true);
    node.classList.toggle("validation-result--bad", ok === false);
  }

  function setState(card, text, kind) {
    const node = card.querySelector("[data-state]");
    node.innerHTML = "";
    if (kind) node.appendChild(el("span", { class: `dot dot--${kind}` }));
    node.appendChild(document.createTextNode(text));
  }

  function renderModels(card, models) {
    const list = card.querySelector("[data-model-list]");
    const showAll = card.querySelector("[data-show-all]");
    list.innerHTML = "";
    let hidden = 0;
    models.forEach((m) => {
      const label = el("label", { class: "model-option" + (m.recommended ? " model-option--rec" : "") });
      const box = el("input", { type: "checkbox", value: m.id });
      box.checked = !!m.selected;
      label.appendChild(box);
      label.appendChild(el("span", {}, m.id));
      if (m.recommended) label.appendChild(el("em", { class: "model-tag" }, "Recommended"));
      // Recommended and already-ticked models first; the rest on request.
      if (!m.recommended && !m.selected) { label.hidden = true; label.dataset.extra = "1"; hidden += 1; }
      list.appendChild(label);
    });
    showAll.hidden = hidden === 0;
    showAll.textContent = `Show all models (${hidden} more)`;
  }

  let saveTimer = null;
  async function saveSelection(card) {
    const pid = card.dataset.provider;
    const ticked = Array.from(card.querySelectorAll("[data-model-list] input:checked")).map((b) => b.value);
    if (!ticked.length) {
      setResult(card, "Keep at least one model ticked, or remove this provider.", false);
      return;
    }
    const data = await post(`/api/ai/${pid}/models`, { models: ticked });
    setResult(card, data.message, !!data.ok);
  }

  document.querySelectorAll(".provider-card").forEach((card) => {
    const pid = card.dataset.provider;
    const connectBox = card.querySelector("[data-connect]");
    const modelsBox = card.querySelector("[data-models]");
    const keyInput = card.querySelector("[data-key]");
    const baseInput = card.querySelector("[data-base-url]");
    const connectBtn = card.querySelector("[data-connect-btn]");

    if (card.classList.contains("is-connected")) {
      getJSON(`/api/ai/${pid}/models`).then((data) => {
        if (data.models && data.models.length) renderModels(card, data.models);
        if (!data.ok && data.rejected) {
          setState(card, "Key rejected", "bad");
          setResult(card, `${card.dataset.label} no longer accepts this key. Replace it below.`, false);
          connectBox.hidden = false;
        } else if (!data.ok && data.message) {
          setResult(card, `Couldn't load the live model list: ${data.message}`, false);
        }
      });
    }

    connectBtn.addEventListener("click", async () => {
      const key = keyInput.value.trim();
      if (!key) { setResult(card, "Paste your key first.", false); keyInput.focus(); return; }
      connectBtn.disabled = true;
      connectBtn.textContent = "Checking…";
      setResult(card, "");
      const data = await post(`/api/ai/${pid}/connect`, { api_key: key, base_url: baseInput ? baseInput.value.trim() : "" });
      connectBtn.disabled = false;
      connectBtn.textContent = "Connect";
      setResult(card, data.message, !!data.ok);
      if (!data.ok) return;
      keyInput.value = "";
      // Reload so the summary, the pool and "Try first" include this provider.
      setTimeout(() => window.location.reload(), 900);
    });
    keyInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); connectBtn.click(); } });

    modelsBox.addEventListener("change", (e) => {
      if (e.target.type !== "checkbox") return;
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => saveSelection(card), 400);
    });
    card.querySelector("[data-show-all]").addEventListener("click", (e) => {
      card.querySelectorAll("[data-extra]").forEach((n) => { n.hidden = false; });
      e.currentTarget.hidden = true;
    });
    card.querySelector("[data-replace-key]").addEventListener("click", () => {
      connectBox.hidden = false;
      keyInput.focus();
    });
    card.querySelector("[data-disconnect]").addEventListener("click", async () => {
      if (!window.confirm(`Remove ${card.dataset.label}? Its key is deleted and its models leave your pool.`)) return;
      const data = await post(`/api/ai/${pid}/disconnect`, {});
      if (data.ok) window.location.reload();
      else setResult(card, data.message, false);
    });
  });

  const primarySelect = $("ai-primary");
  if (primarySelect) {
    primarySelect.addEventListener("change", async () => {
      const data = await post("/api/ai/primary", { model: primarySelect.value });
      const hint = primarySelect.parentElement.querySelector(".field-hint");
      if (hint) hint.textContent = data.ok ? `✓ ${data.message} The others take over whenever it's busy.` : `✗ ${data.message}`;
    });
  }

  // ── Which mail service hosts this address? ───────────────────────────────
  // A university address rarely says whether it's Microsoft 365, Google or
  // the school's own server; the server looks it up and we show the one way
  // to connect that works, filling in SMTP settings when that's the way.
  const detectBtn = $("mail-detect-btn");
  if (detectBtn) {
    const detectInput = $("mail-detect-input");
    const out = $("mail-detect-result");
    const pickMethod = (value) => {
      const radio = document.querySelector(`input[name="mail_method"][value="${value}"]`);
      if (radio && !radio.disabled) { radio.checked = true; radio.dispatchEvent(new Event("change", { bubbles: true })); }
    };
    const line = (html) => { const p = el("p"); p.innerHTML = html; out.appendChild(p); };
    const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const linkButton = (href, label) => {
      const a = el("a", { class: "button-google", href });
      a.textContent = label;
      out.appendChild(a);
    };

    const run = async () => {
      const address = detectInput.value.trim();
      if (!address) { detectInput.focus(); return; }
      detectBtn.disabled = true;
      detectBtn.textContent = "Checking…";
      out.hidden = false;
      out.innerHTML = "";
      try {
        const data = await getJSON(`/api/mail/detect?email=${encodeURIComponent(address)}`);
        if (!data.ok) { line(`✗ ${esc(data.message || "Couldn't check that address.")}`); return; }
        const who = `<strong>${esc(address)}</strong> is ${data.kind === "smtp" ? "on" : "a"} <strong>${esc(data.label)}</strong>${data.kind === "smtp" ? "" : " mailbox"}${data.school ? " (your school's)" : ""}.`;
        line(who);
        if (data.kind === "microsoft") {
          if (data.available.microsoft) {
            line("Sign in once with Microsoft — no password to copy, and it keeps working after Microsoft switches off password-based sending.");
            linkButton("/oauth/microsoft/start", "Connect with Microsoft");
            pickMethod("microsoft");
          } else {
            line("Microsoft sign-in isn't enabled on this platform yet — ask the administrator to turn it on (Admin → Platform). It's free.");
          }
        } else if (data.kind === "google") {
          if (data.available.google) {
            line("Sign in once with Google — no password to copy.");
            linkButton("/oauth/start", "Sign in with Google");
            pickMethod("oauth");
          } else {
            line("Use a Google app password below (needs 2-Step Verification on the account).");
            pickMethod("app_password");
            $("gmail-address").value = address;
          }
        } else {
          pickMethod("smtp");
          $("gmail-address").value = address;
          if (data.smtp) {
            $("smtp-host").value = data.smtp.host;
            $("smtp-port").value = data.smtp.port;
            $("smtp-security").value = data.smtp.security;
          }
          if (data.imap_host) $("imap-host").value = data.imap_host;
          line(data.smtp && data.smtp.guess
            ? "We filled in the usual server names — check them on your school's IT help page, then enter your mailbox password and press <em>Test login</em>."
            : "We filled in the server settings. Enter your password (or an app password) and press <em>Test login</em>.");
          if (data.app_password_url) line(`This provider needs an app password: <a href="${esc(data.app_password_url)}" target="_blank" rel="noopener noreferrer">create one here</a>.`);
        }
      } finally {
        detectBtn.disabled = false;
        detectBtn.textContent = "Find how to connect it";
      }
    };
    detectBtn.addEventListener("click", run);
    detectInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } });
  }

  // ── Email method: show only the fields that method needs ─────────────────
  const radios = Array.from(document.querySelectorAll('input[name="mail_method"]'));
  function paintMethod() {
    const method = (radios.find((r) => r.checked) || {}).value || "";
    document.querySelectorAll(".method-panel").forEach((panel) => {
      panel.hidden = !panel.dataset.method.split(" ").includes(method);
    });
    document.querySelectorAll("[data-method-only]").forEach((node) => {
      node.hidden = node.dataset.methodOnly !== method;
    });
    document.querySelectorAll("[data-method-label]").forEach((node) => {
      node.hidden = node.dataset.methodLabel !== method;
    });
    document.querySelectorAll(".method-card").forEach((card) => {
      card.classList.toggle("method-card--active", !!card.querySelector("input:checked"));
    });
  }
  radios.forEach((r) => r.addEventListener("change", paintMethod));
  if (radios.length) paintMethod();

  // ── Companies import ─────────────────────────────────────────────────────
  const fileInput = $("companies-file");
  const report = $("import-report");
  const previewBtn = $("import-preview-btn");

  function mode() {
    return (document.querySelector('input[name="mode"]:checked') || {}).value || "append";
  }

  function formData() {
    const data = new FormData();
    data.append("companies_file", fileInput.files[0]);
    data.append("mode", mode());
    return data;
  }

  function renderReport(data) {
    report.hidden = false;
    report.innerHTML = "";
    report.className = "import-report" + (data.ok ? "" : " import-report--bad");
    if (!data.ok) {
      report.appendChild(el("p", { class: "import-report__title" }, "✗ " + data.message));
      return;
    }
    const replacing = mode() === "replace";
    const willAdd = replacing ? data.usable : data.new;
    report.appendChild(el("p", { class: "import-report__title" },
      willAdd ? `✓ ${willAdd} compan${willAdd === 1 ? "y" : "ies"} ready to ${replacing ? "replace your list" : "add"}.`
              : "Nothing new to add — every address is already in your list."));
    const stats = el("ul", { class: "import-stats" });
    [["rows in the file", data.total], ["usable", data.usable],
     ["already in your list", replacing ? 0 : data.already_listed],
     ["duplicates inside the file", data.duplicates], ["invalid or missing email", data.invalid]]
      .forEach(([label, value]) => {
        const li = el("li");
        li.appendChild(el("strong", {}, String(value)));
        li.appendChild(document.createTextNode(" " + label));
        stats.appendChild(li);
      });
    report.appendChild(stats);
    const found = Object.entries(data.columns || {}).map(([std, orig]) => `${std} ← “${orig}”`).join(" · ");
    if (found) report.appendChild(el("p", { class: "field-hint" }, "Columns understood: " + found));
    if (data.errors && data.errors.length) {
      report.appendChild(el("p", { class: "field-hint" }, "Rows to fix in your file:"));
      const list = el("ul", { class: "import-errors" });
      data.errors.forEach((e) => list.appendChild(el("li", {}, `Row ${e.row}${e.email ? " (" + e.email + ")" : ""}: ${e.reason}`)));
      report.appendChild(list);
    }
    if (willAdd) {
      const actions = el("div", { class: "form-actions" });
      const importBtn = el("button", { type: "button", class: "button button--primary" },
        replacing ? `Replace my list with ${willAdd}` : `Import ${willAdd}`);
      importBtn.addEventListener("click", () => runImport(importBtn));
      actions.appendChild(importBtn);
      report.appendChild(actions);
    }
  }

  async function runImport(button) {
    if (mode() === "replace" && !window.confirm("Replace your whole list with this file? Drafts and sent history are kept.")) return;
    button.disabled = true;
    button.textContent = "Importing…";
    const data = await post("/api/companies/import", formData(), true);
    report.innerHTML = "";
    report.className = "import-report" + (data.ok ? " import-report--ok" : " import-report--bad");
    report.appendChild(el("p", { class: "import-report__title" }, (data.ok ? "✓ " : "✗ ") + data.message));
    if (data.ok && $("companies-count")) $("companies-count").textContent = data.total;
  }

  if (previewBtn && fileInput) {
    previewBtn.addEventListener("click", async () => {
      if (!fileInput.files.length) {
        renderReport({ ok: false, message: "Choose a .csv or .xlsx file first." });
        return;
      }
      previewBtn.disabled = true;
      previewBtn.textContent = "Checking…";
      try {
        renderReport(await post("/api/companies/preview", formData(), true));
      } finally {
        previewBtn.disabled = false;
        previewBtn.textContent = "Check file";
      }
    });
    fileInput.addEventListener("change", () => { report.hidden = true; });
    document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", () => {
      if (!report.hidden && fileInput.files.length) previewBtn.click();
    }));
  }

  const clearBtn = $("clear-companies-btn");
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (!window.confirm("Clear your companies list? Drafts and sent history are kept.")) return;
      const data = await post("/api/companies/clear", {});
      if (data.ok) window.location.reload();
      else window.alert(data.message);
    });
  }
})();
