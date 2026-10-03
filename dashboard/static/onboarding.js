/* First-time setup: the small interactions of each step. Every step also
   works as a plain page — this only removes waiting and reloads. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const UNREACHABLE = "Couldn't reach Ntern — check your connection and try again.";

  async function post(url, body, isForm) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: isForm ? { "X-CSRF-Token": csrfToken() }
                        : { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: isForm ? body : JSON.stringify(body || {}),
      });
    } catch (_) {
      return { ok: false, message: UNREACHABLE };
    }
    if (response.status === 401) { window.location.href = "/login"; return { ok: false, message: "Session ended." }; }
    try { return await response.json(); } catch (_) { return { ok: false, message: `Server error (${response.status}).` }; }
  }

  function say(node, text, ok) {
    if (!node) return;
    node.textContent = text || "";
    node.classList.toggle("validation-result--ok", ok === true);
    node.classList.toggle("validation-result--bad", ok === false);
  }

  // ── CV: upload, then the check appears ───────────────────────────────────
  const cvForm = $("cv-form");
  if (cvForm) {
    const input = $("cv-file");
    const upload = async () => {
      if (!input.files.length) return;
      const data = new FormData();
      data.append("cv_file", input.files[0]);
      cvForm.classList.add("is-busy");
      say($("cv-result"), "Checking your CV…");
      const reply = await post("/api/cv/check", data, true);
      cvForm.classList.remove("is-busy");
      if (!reply.ok) { say($("cv-result"), reply.message, false); return; }
      window.location.href = "/welcome?step=cv";     // the report is rendered by the server
    };
    input.addEventListener("change", upload);
    ["dragenter", "dragover"].forEach((name) => cvForm.addEventListener(name, () => cvForm.classList.add("is-over")));
    ["dragleave", "drop"].forEach((name) => cvForm.addEventListener(name, () => cvForm.classList.remove("is-over")));
  }

  // ── Places: show more countries, count the choice ────────────────────────
  const places = $("places-form");
  if (places) {
    const count = () => {
      const n = places.querySelectorAll('input[name="country"]:checked').length;
      $("places-count").textContent = n ? `${n} place${n === 1 ? "" : "s"} selected.` : "Nothing selected — every country is treated the same.";
    };
    places.addEventListener("change", count);
    count();
    $("more-countries").addEventListener("click", (e) => {
      places.querySelectorAll(".ob-chip--more").forEach((chip) => { chip.hidden = false; });
      e.currentTarget.hidden = true;
    });
  }

  // ── AI key: connect, then move on ────────────────────────────────────────
  document.querySelectorAll(".ob-providers .provider-card").forEach((card) => {
    const button = card.querySelector("[data-connect-btn]");
    const input = card.querySelector("[data-key]");
    if (!button) return;
    const connect = async () => {
      const key = input.value.trim();
      const note = card.querySelector("[data-result]");
      if (!key) { say(note, "Paste your key first.", false); input.focus(); return; }
      button.disabled = true;
      button.textContent = "Checking…";
      say(note, "");
      const reply = await post(`/api/ai/${card.dataset.provider}/connect`, { api_key: key });
      button.disabled = false;
      button.textContent = "Connect";
      say(note, reply.message, !!reply.ok);
      if (reply.ok) setTimeout(() => { window.location.href = "/welcome?step=ai"; }, 700);
    };
    button.addEventListener("click", connect);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); connect(); } });
  });

  // ── Profile: the analysis takes a while — say so ─────────────────────────
  const analyze = $("analyze-form");
  if (analyze) {
    analyze.addEventListener("submit", () => {
      const button = $("analyze-btn");
      button.disabled = true;
      button.textContent = "Reading your CV…";
      let seconds = 0;
      const note = $("analyze-note");
      setInterval(() => {
        seconds += 1;
        note.textContent = seconds < 20 ? `Working… ${seconds}s (usually 20–60 seconds)`
          : `Still working… ${seconds}s — free AI models can be slow, please keep this page open.`;
      }, 1000);
    });
  }

  // ── Style: choose, preview, save ─────────────────────────────────────────
  const styleSave = $("style-save");
  if (styleSave) {
    const chosen = () => (document.querySelector('input[name="template_id"]:checked') || {}).value;
    document.querySelectorAll('input[name="template_id"]').forEach((radio) => radio.addEventListener("change", () => {
      document.querySelectorAll(".template-card").forEach((c) => c.classList.toggle("template-card--active", c.contains(radio) && radio.checked));
    }));
    $("style-preview-btn").addEventListener("click", async () => {
      const reply = await post("/api/profile/preview", { template_id: chosen() });
      const box = $("style-preview");
      if (!reply.ok) { say($("style-result"), reply.message, false); return; }
      box.innerHTML = "";
      box.hidden = false;
      ["en", "fr"].forEach((lang) => {
        const item = reply.preview[lang];
        if (!item) return;
        const article = document.createElement("article");
        article.className = "email-preview";
        const head = document.createElement("div");
        head.className = "email-preview__headers";
        const key = document.createElement("span"); key.className = "email-preview__key"; key.textContent = lang.toUpperCase();
        const subject = document.createElement("span"); subject.className = "email-preview__val email-preview__subject";
        subject.textContent = item.error ? "Can't build this email yet" : item.subject;
        head.append(key, subject);
        const body = document.createElement("div"); body.className = "email-preview__body";
        body.textContent = item.error || `${item.body}\n\n— ${item.words} words`;
        article.append(head, body);
        box.append(article);
      });
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
    styleSave.addEventListener("click", async () => {
      styleSave.disabled = true;
      const reply = await post("/api/profile/settings", { template_id: chosen(), language_mode: $("language-mode").value });
      if (!reply.ok) { styleSave.disabled = false; say($("style-result"), reply.message, false); return; }
      await post("/welcome/seen/style", {});
      window.location.href = styleSave.dataset.next;
    });
  }
})();
