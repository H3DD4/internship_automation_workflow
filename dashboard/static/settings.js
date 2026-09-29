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
