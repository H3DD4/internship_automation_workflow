/* Profile page: the CV-grounded editor (English + French side by side),
   flags for claims the CV doesn't back up, style/language choice, preview. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
  const LANGS = ["en", "fr"];
  const LANG_LABEL = { en: "English", fr: "Français" };

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
      body: JSON.stringify(body || {}),
    });
    if (response.status === 401) { window.location.href = "/login"; return { ok: false, message: "Session ended." }; }
    try { return await response.json(); } catch (_) { return { ok: false, message: `Server error (${response.status}).` }; }
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === false) return;
      if (k === "text") node.textContent = v;
      else if (k === "dataset") Object.assign(node.dataset, v);
      else node.setAttribute(k, v === true ? "" : v);
    });
    children.flat().forEach((c) => { if (c) node.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return node;
  }

  function setResult(id, text, ok) {
    const node = $(id);
    if (!node) return;
    node.textContent = text;
    node.classList.remove("validation-result--ok", "validation-result--bad");
    if (ok !== undefined) node.classList.add(ok ? "validation-result--ok" : "validation-result--bad");
  }

  const dataNode = $("profile-data");
  const initial = dataNode ? JSON.parse(dataNode.textContent) : { facts: null, flags: [] };
  let facts = initial.facts;
  const confirmed = new Set((facts && facts.confirmed) || []);

  // ── Field builders ────────────────────────────────────────────────────────
  let uid = 0;
  function pair(label, value, flagPrefix, opts = {}) {
    const wrap = el("div", { class: "pair" + (opts.wide ? " pair--wide" : "") });
    wrap.appendChild(el("div", { class: "pair__label", text: label }));
    if (opts.hint) wrap.appendChild(el("div", { class: "field-hint", text: opts.hint }));
    const row = el("div", { class: "pair__row" });
    LANGS.forEach((lang) => {
      const id = `f${++uid}`;
      const field = opts.single
        ? el("input", { id, type: "text", maxlength: 300, value: (value || {})[lang] || "", "data-lang": lang })
        : el("textarea", { id, rows: opts.rows || 2, maxlength: 1200, "data-lang": lang });
      if (!opts.single) field.value = (value || {})[lang] || "";
      if (flagPrefix) field.dataset.flagKey = `${flagPrefix}.${lang}`;
      row.appendChild(el("label", { class: "pair__cell", for: id },
        el("span", { class: "pair__lang", text: LANG_LABEL[lang] }), field,
        el("span", { class: "flag", hidden: true })));
    });
    wrap.appendChild(row);
    wrap.readValue = () => Object.fromEntries(Array.from(row.querySelectorAll("[data-lang]"))
      .map((f) => [f.dataset.lang, f.value.trim()]));
    return wrap;
  }

  function textField(label, value, opts = {}) {
    const id = `f${++uid}`;
    const input = el("input", { id, type: "text", maxlength: opts.max || 300, value: value || "" });
    const wrap = el("div", { class: "form-group" + (opts.wide ? " form-group--wide" : "") },
      el("label", { for: id, text: label }), input, opts.hint ? el("span", { class: "field-hint", text: opts.hint }) : null);
    wrap.readValue = () => input.value.trim();
    return wrap;
  }

  const splitList = (text) => text.split(",").map((s) => s.trim()).filter(Boolean);

  // ── Areas and strengths ───────────────────────────────────────────────────
  function areaCard(area, index) {
    area = area || { id: `area_${Date.now().toString(36)}`, label: {}, topic: {}, evidence: {}, alt_evidence: {},
                     keywords: [], keywords_fr: [], sources: [], alt_sources: [] };
    const prefix = `area.${area.id}`;
    const card = el("fieldset", { class: "item-card" });
    const title = el("legend", { class: "item-card__title", text: (area.label && area.label.en) || "New area" });
    const label = pair("Name of the field (reads after “Your focus on …” / “Votre expertise en …”)", area.label, `${prefix}.label`, { single: true });
    const topic = pair("Subject-line topic", area.topic, null, { single: true });
    const match = textField("What kind of company this fits (English, used for research)", area.match_description, { wide: true, max: 300 });
    const kw = textField("Keywords a matching company's website would use (English, comma-separated)", (area.keywords || []).join(", "), { wide: true, max: 1200 });
    const kwFr = textField("The same keywords in French", (area.keywords_fr || []).join(", "), { wide: true, max: 1200 });
    const evidence = pair("Your evidence — one sentence with a concrete result", area.evidence, `${prefix}.evidence`, { rows: 3, wide: true });
    const alt = pair("Alternative evidence from a different project (optional)", area.alt_evidence, `${prefix}.alt_evidence`, { rows: 2, wide: true });
    const remove = el("button", { type: "button", class: "button button--ghost button--sm text-danger", text: "Remove this area" });
    remove.addEventListener("click", () => { card.remove(); renumber(); });
    label.addEventListener("input", () => { title.textContent = label.readValue().en || "New area"; });
    card.append(title, label, topic, match, kw, kwFr, evidence, alt, el("div", { class: "item-card__actions" }, remove));
    card.readValue = () => ({
      id: area.id, label: label.readValue(), topic: topic.readValue(), match_description: match.readValue(),
      keywords: splitList(kw.readValue()), keywords_fr: splitList(kwFr.readValue()),
      requires_any: area.requires_any || [], requires_any_fr: area.requires_any_fr || [],
      evidence: evidence.readValue(), sources: area.sources && area.sources.length ? area.sources : [area.id],
      alt_evidence: alt.readValue(), alt_sources: area.alt_sources && area.alt_sources.length ? area.alt_sources : [`${area.id}_alt`],
    });
    return card;
  }

  function strengthCard(strength) {
    strength = strength || { id: `strength_${Date.now().toString(36)}`, text: {}, sources: [] };
    const card = el("fieldset", { class: "item-card item-card--compact" });
    const text = pair("Strength", strength.text, `strength.${strength.id}`, { rows: 2, wide: true });
    const up = el("button", { type: "button", class: "button button--ghost button--sm", text: "↑ Move up" });
    const remove = el("button", { type: "button", class: "button button--ghost button--sm text-danger", text: "Remove" });
    up.addEventListener("click", () => { if (card.previousElementSibling) card.parentNode.insertBefore(card, card.previousElementSibling); renumber(); });
    remove.addEventListener("click", () => { card.remove(); renumber(); });
    const legend = el("legend", { class: "item-card__title" });
    card.append(legend, text, el("div", { class: "item-card__actions" }, up, remove));
    card.readValue = () => ({ id: strength.id, text: text.readValue(),
                              sources: strength.sources && strength.sources.length ? strength.sources : [strength.id] });
    return card;
  }

  let areasBox, strengthsBox, basics = {};
  function renumber() {
    if (!strengthsBox) return;
    Array.from(strengthsBox.children).forEach((card, i) => {
      card.querySelector("legend").textContent = i === 0 ? "Strength 1 — your flagship (opens “Project first” emails)" : `Strength ${i + 1}`;
    });
  }

  function renderEditor() {
    const root = $("profile-editor");
    if (!root || !facts) return;
    root.innerHTML = "";
    basics = {
      full_name: textField("Name (signs every email)", facts.full_name, { max: 120 }),
      identity: pair("Who you are — continues “I'm …” / “Je suis …”", facts.identity, "identity",
                     { rows: 2, wide: true, hint: "English starts with “a …”; French has no article (“en dernière année de …”)." }),
      default_topic: pair("Your field, for the subject line", facts.default_topic, "default_topic", { single: true }),
      target_role: pair("The role, for the subject line", facts.target_role, null, { single: true }),
      start_date: pair("Start date", facts.start_date, null, { single: true }),
      internship_ask: pair("What you're asking for", facts.internship_ask, null, { rows: 2, wide: true }),
      motivation: pair("What drives you (optional, one sentence)", facts.motivation, "motivation", { rows: 2, wide: true }),
    };
    const basicsCard = el("div", { class: "editor-section" }, el("h3", { class: "subhead", text: "About you" }),
      ...Object.values(basics));
    areasBox = el("div", { class: "item-list" });
    (facts.areas || []).forEach((a, i) => areasBox.appendChild(areaCard(a, i)));
    const addArea = el("button", { type: "button", class: "button button--ghost button--sm", text: "+ Add an area" });
    addArea.addEventListener("click", () => areasBox.appendChild(areaCard(null)));
    strengthsBox = el("div", { class: "item-list" });
    (facts.strengths || []).forEach((s) => strengthsBox.appendChild(strengthCard(s)));
    const addStrength = el("button", { type: "button", class: "button button--ghost button--sm", text: "+ Add a strength" });
    addStrength.addEventListener("click", () => { strengthsBox.appendChild(strengthCard(null)); renumber(); });
    root.append(basicsCard,
      el("div", { class: "editor-section" }, el("h3", { class: "subhead", text: "Areas of experience" }),
        el("p", { class: "field-hint", text: "When a company's website matches an area, its evidence sentence goes in the email. Aim for 3–8 areas." }),
        areasBox, addArea),
      el("div", { class: "editor-section" }, el("h3", { class: "subhead", text: "Strengths" }),
        el("p", { class: "field-hint", text: "Your strongest points, used in every email (skipping any already cited)." }),
        strengthsBox, addStrength));
    renumber();
    showFlags(initial.flags || []);
  }

  function collect() {
    const read = (key) => basics[key].readValue();
    return {
      full_name: read("full_name"), identity: read("identity"), default_topic: read("default_topic"),
      target_role: read("target_role"), start_date: read("start_date"), internship_ask: read("internship_ask"),
      motivation: read("motivation"), internship: (facts && facts.internship) || {},
      areas: Array.from(areasBox.children).map((c) => c.readValue()),
      strengths: Array.from(strengthsBox.children).map((c) => c.readValue()),
      confirmed: Array.from(confirmed),
    };
  }

  function showFlags(flags) {
    const byKey = Object.fromEntries(flags.map((f) => [f.key, f]));
    document.querySelectorAll("[data-flag-key]").forEach((field) => {
      const key = field.dataset.flagKey;
      const flagBox = field.parentNode.querySelector(".flag");
      const flag = byKey[key];
      field.classList.toggle("input-flagged", !!flag);
      flagBox.hidden = !flag;
      flagBox.innerHTML = "";
      if (!flag) return;
      const box = el("input", { type: "checkbox" });
      box.checked = confirmed.has(key);
      box.addEventListener("change", () => { if (box.checked) confirmed.add(key); else confirmed.delete(key); });
      flagBox.append(el("span", { text: "⚠ " + flag.reason }),
        el("label", { class: "flag__confirm" }, box, " This is true"));
    });
    const first = document.querySelector(".input-flagged");
    return first;
  }

  // ── Save profile ──────────────────────────────────────────────────────────
  const saveBtn = $("save-profile-btn");
  if (saveBtn) {
    renderEditor();
    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      setResult("profile-result", "Checking every sentence against your CV…");
      const template = (document.querySelector('input[name="template_id"]:checked') || {}).value;
      const data = await postJSON("/api/profile/save", { facts: collect(),
        template_id: template && template !== "custom" ? template : undefined });
      saveBtn.disabled = false;
      setResult("profile-result", (data.ok ? "✓ " : "✗ ") + data.message, data.ok);
      const first = showFlags(data.flags || []);
      if (!data.ok && first) first.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  // ── Style, language, preview ──────────────────────────────────────────────
  document.querySelectorAll(".template-card input").forEach((radio) => {
    radio.addEventListener("change", () => {
      document.querySelectorAll(".template-card").forEach((card) =>
        card.classList.toggle("template-card--active", !!card.querySelector("input:checked")));
    });
  });
  const chosenTemplate = () => (document.querySelector('input[name="template_id"]:checked') || {}).value;

  const styleBtn = $("save-style-btn");
  if (styleBtn) {
    styleBtn.addEventListener("click", async () => {
      styleBtn.disabled = true;
      const data = await postJSON("/api/profile/settings", { template_id: chosenTemplate(),
        language_mode: ($("language-mode") || {}).value });
      styleBtn.disabled = false;
      setResult("style-result", (data.ok ? "✓ " : "✗ ") + data.message, data.ok);
    });
  }

  const previewBtn = $("preview-btn");
  if (previewBtn) {
    previewBtn.addEventListener("click", async () => {
      previewBtn.disabled = true;
      setResult("style-result", "Rendering…");
      const payload = { template_id: chosenTemplate() };
      if (areasBox && chosenTemplate() !== "custom") payload.facts = collect();
      const data = await postJSON("/api/profile/preview", payload);
      previewBtn.disabled = false;
      if (!data.ok) { setResult("style-result", "✗ " + data.message, false); return; }
      setResult("style-result", `Preview for ${data.company} — nothing is saved.`);
      const pairBox = $("preview-pair");
      pairBox.hidden = false;
      LANGS.forEach((lang) => {
        const item = data.preview[lang];
        const subject = pairBox.querySelector(`[data-preview-subject="${lang}"]`);
        const body = pairBox.querySelector(`[data-preview-body="${lang}"]`);
        if (!item) { subject.textContent = "—"; body.textContent = "No wording in this language yet."; return; }
        if (item.error) { subject.textContent = "Can't build this email yet"; body.textContent = item.error; return; }
        subject.textContent = item.subject;
        body.textContent = item.body + `\n\n— ${item.words} words`;
      });
      pairBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
    });
  }

  // ── Hand-written wording ──────────────────────────────────────────────────
  const customBtn = $("save-custom-btn");
  if (customBtn) {
    customBtn.addEventListener("click", async () => {
      customBtn.disabled = true;
      const data = await postJSON("/api/profile/custom-spec", { spec_en: $("spec-en").value, spec_fr: $("spec-fr").value });
      customBtn.disabled = false;
      setResult("custom-result", (data.ok ? "✓ " : "✗ ") + data.message, data.ok);
    });
  }

  // ── Analyse: it takes a while, say so ─────────────────────────────────────
  const analyzeForm = $("analyze-form");
  if (analyzeForm) {
    analyzeForm.addEventListener("submit", () => {
      const btn = $("analyze-btn");
      btn.disabled = true;
      btn.textContent = "Analysing your CV…";
      $("analyze-note").textContent = "Reading your CV and drafting both languages — keep this tab open.";
    });
  }

  // ── Update every draft waiting for review with the current profile ───────
  const rebuildBtn = document.getElementById("rebuild-ready-btn");
  if (rebuildBtn) {
    rebuildBtn.addEventListener("click", async () => {
      const n = rebuildBtn.dataset.count;
      if (!window.confirm(`Rewrite your ${n} draft(s) waiting for review with your current profile and dates?\n\n` +
          "It's instant and uses no AI, but any edits you made by hand to those drafts are replaced. " +
          "Sent emails are never changed.")) return;
      rebuildBtn.disabled = true;
      rebuildBtn.textContent = "Updating…";
      const result = document.getElementById("rebuild-ready-result");
      const data = await postJSON("/api/drafts/rebuild-ready", {});
      result.textContent = (data.ok ? "✓ " : "✗ ") + data.message;
      rebuildBtn.textContent = data.ok ? "Done" : "Try again";
      rebuildBtn.disabled = !!data.ok;
    });
  }
})();
