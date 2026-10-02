/* Profile page: the student's own template — paste an email you like and
   Ntern splits it into sections, or start from a blank one; then reorder the
   sections, change any wording with friendly blanks, preview and save. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const dataNode = $("own-data");
  if (!dataNode) return;
  const DATA = JSON.parse(dataNode.textContent);
  const csrfToken = () => (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

  async function postJSON(url, body) {
    let response;
    try {
      response = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
        body: JSON.stringify(body || {}),
      });
    } catch (_) {
      return { ok: false, message: "Couldn't reach Ntern — check your connection, reload the page and try again." };
    }
    if (response.status === 401) { window.location.href = "/login"; return { ok: false, message: "Session ended." }; }
    try { return await response.json(); } catch (_) { return { ok: false, message: `Server error (${response.status}).` }; }
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v === undefined || v === null || v === false) return;
      if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : v);
    });
    children.flat().forEach((c) => c !== null && c !== undefined && node.append(c));
    return node;
  }

  const LANGS = ["en", "fr"];
  const LANG_LABEL = { en: "English", fr: "Français" };
  // The sections a student can place, in plain words.
  const SECTIONS = {
    intro: { title: "Opening", hint: "Why you're writing to them. Two versions: one for when Ntern found what the company does on its website, one for when it didn't.", fixed: true },
    match: { title: "Why you match", hint: "One sentence that leads into your experience — the matching sentences from your CV follow it automatically." },
    strengths: { title: "Your highlights", hint: "Your strongest results from your CV, added automatically." },
    about: { title: "About you", hint: "A paragraph in your own words, the same in every email. Only claims your CV backs up." },
    ask: { title: "What you ask for", hint: "Leave empty to use the sentence from your dates card, or write your own with the blanks." },
    closing: { title: "Closing", hint: "Your last line — e.g. your CV is attached, a question, a thank-you." },
  };
  const FIELD_LABEL = {
    subject: "Subject", intro_with_hook: "When Ntern found what they do", intro_standard: "When it didn't",
    match_lead: "Lead sentence", about: "Your paragraph", ask: "Your sentence", closing: "Closing line", sign_off: "Sign-off",
  };

  let state = null;
  let lang = "en";
  let lastField = null;       // the textarea a blank is inserted into

  const editor = $("own-editor");
  const actions = $("own-actions");
  const start = $("own-start");
  const result = $("own-result");
  const problemsBox = $("own-problems");
  const previewBox = $("own-preview-pair");

  const clone = (o) => JSON.parse(JSON.stringify(o));
  const texts = () => (state[lang] = state[lang] || {});

  function setResult(node, text, ok) {
    node.textContent = text || "";
    node.classList.toggle("validation-result--ok", ok === true);
    node.classList.toggle("validation-result--bad", ok === false);
  }

  function showProblems(list) {
    problemsBox.innerHTML = "";
    problemsBox.hidden = !(list && list.length);
    (list || []).forEach((p) => problemsBox.append(el("li", { text: p })));
  }

  function chips(key) {
    const row = el("div", { class: "own-chips", "aria-label": "Blanks Ntern fills in" });
    (DATA.fieldBlanks[key] || []).forEach((blank) => {
      const chip = el("button", { type: "button", class: "own-chip", text: blank, title: "Insert " + blank });
      // mousedown keeps the textarea's cursor where it was.
      chip.addEventListener("mousedown", (e) => e.preventDefault());
      chip.addEventListener("click", () => insertBlank(key, blank));
      row.append(chip);
    });
    return row;
  }

  function insertBlank(key, blank) {
    const field = editor.querySelector(`[data-field="${key}"]`);
    if (!field) return;
    const s = field.selectionStart ?? field.value.length;
    const e = field.selectionEnd ?? field.value.length;
    field.value = field.value.slice(0, s) + blank + field.value.slice(e);
    field.focus();
    field.setSelectionRange(s + blank.length, s + blank.length);
    texts()[key] = field.value;
  }

  function textField(key, rows) {
    const id = `own-${lang}-${key}`;
    const input = rows === 1
      ? el("input", { type: "text", id, "data-field": key, maxlength: "300" })
      : el("textarea", { id, "data-field": key, rows: String(rows || 3), maxlength: "1500" });
    input.value = texts()[key] || "";
    input.addEventListener("input", () => { texts()[key] = input.value; });
    input.addEventListener("focus", () => { lastField = key; });
    return el("div", { class: "own-field" }, el("label", { for: id, text: FIELD_LABEL[key] }), input, chips(key));
  }

  function move(index, delta) {
    const target = index + delta;
    if (target < 0 || target >= state.layout.length) return;
    [state.layout[index], state.layout[target]] = [state.layout[target], state.layout[index]];
    render();
  }

  function sectionCard(section, index) {
    const meta = SECTIONS[section];
    const tools = el("div", { class: "own-section__tools" },
      el("button", { type: "button", class: "icon-btn", "aria-label": "Move up", title: "Move up", disabled: index === 0 || undefined, text: "↑" }),
      el("button", { type: "button", class: "icon-btn", "aria-label": "Move down", title: "Move down", disabled: index === state.layout.length - 1 || undefined, text: "↓" }),
      meta.fixed ? null : el("button", { type: "button", class: "icon-btn icon-btn--danger", "aria-label": "Remove section", title: "Remove", text: "✕" }));
    const [up, down, remove] = tools.querySelectorAll("button");
    up.addEventListener("click", () => move(index, -1));
    down.addEventListener("click", () => move(index, 1));
    if (remove) remove.addEventListener("click", () => { state.layout.splice(index, 1); render(); });

    const body = el("div", { class: "own-section__body" });
    if (section === "intro") body.append(textField("intro_with_hook", 3), textField("intro_standard", 2));
    else if (section === "match") body.append(textField("match_lead", 2));
    else if (section === "about") body.append(textField("about", 4));
    else if (section === "ask") body.append(textField("ask", 2));
    else if (section === "closing") body.append(textField("closing", 2));
    else if (section === "strengths") {
      const select = el("select", { id: "own-highlights" },
        ...[0, 1, 2, 3].map((n) => el("option", { value: String(n), text: n === 0 ? "None" : `${n} highlight${n > 1 ? "s" : ""}` })));
      select.value = String(state.highlights ?? 2);
      select.addEventListener("change", () => { state.highlights = Number(select.value); });
      body.append(el("div", { class: "own-field own-field--inline" }, el("label", { for: "own-highlights", text: "How many" }), select));
    }
    return el("article", { class: "own-section" },
      el("header", { class: "own-section__head" },
        el("span", { class: "own-section__num", text: String(index + 1) }),
        el("div", {}, el("strong", { text: meta.title }), el("p", { class: "field-hint", text: meta.hint })),
        tools),
      body);
  }

  function render() {
    editor.innerHTML = "";
    const tabs = el("div", { class: "segmented own-langs", role: "tablist" });
    LANGS.forEach((l) => {
      const filled = state[l] && Object.values(state[l]).some((v) => v && String(v).trim());
      const tab = el("button", { type: "button", role: "tab", class: "segmented__item" + (l === lang ? " segmented__item--active" : ""),
        "aria-selected": l === lang ? "true" : "false", text: LANG_LABEL[l] + (filled ? "" : " (empty)") });
      tab.addEventListener("click", () => { lang = l; render(); });
      tabs.append(tab);
    });
    editor.append(tabs);
    editor.append(el("p", { class: "field-hint", text: "Click a blank to insert it where your cursor is. Ntern fills it in for each company." }));
    editor.append(textField("subject", 1));
    const list = el("div", { class: "own-sections" });
    state.layout.forEach((section, i) => list.append(sectionCard(section, i)));
    editor.append(list);

    const missing = Object.keys(SECTIONS).filter((s) => !state.layout.includes(s));
    if (missing.length) {
      const select = el("select", { id: "own-add", "aria-label": "Add a section" },
        el("option", { value: "", text: "+ Add a section…" }),
        ...missing.map((s) => el("option", { value: s, text: SECTIONS[s].title })));
      select.addEventListener("change", () => {
        if (!select.value) return;
        state.layout.splice(Math.max(0, state.layout.length - 1), 0, select.value);   // before the closing
        render();
      });
      editor.append(el("div", { class: "own-add" }, select));
    }
    editor.append(textField("sign_off", 2));
  }

  function open(template) {
    state = clone(template);
    state.layout = state.layout && state.layout.length ? state.layout : ["intro", "match", "strengths", "ask", "closing"];
    LANGS.forEach((l) => { state[l] = state[l] || {}; });
    lang = Object.values(state.en).some((v) => v) ? "en" : "fr";
    start.hidden = true;
    editor.hidden = false;
    actions.hidden = false;
    render();
  }

  function showPreview(data) {
    previewBox.innerHTML = "";
    previewBox.hidden = false;
    LANGS.forEach((l) => {
      const item = (data.preview || {})[l];
      if (!item) return;
      previewBox.append(el("article", { class: "email-preview" },
        el("div", { class: "email-preview__headers" },
          el("span", { class: "email-preview__key", text: l.toUpperCase() }),
          el("span", { class: "email-preview__val email-preview__subject", text: item.error ? "Can't build this email yet" : item.subject })),
        el("div", { class: "email-preview__body", text: item.error || `${item.body}\n\n— ${item.words} words · sample: ${data.company}` })));
    });
    previewBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ── Start: paste an example, or a blank template ─────────────────────────
  const convert = $("own-convert");
  if (convert) {
    convert.addEventListener("click", async () => {
      const example = $("own-example").value.trim();
      const note = $("own-convert-result");
      if (example.split(/\s+/).length < 25) { setResult(note, "Paste a whole email — at least a few sentences.", false); return; }
      convert.disabled = true;
      convert.textContent = "Reading your email…";
      setResult(note, "Takes 10–40 seconds.");
      const data = await postJSON("/api/own-template/from-example", { example });
      convert.disabled = false;
      convert.textContent = "Turn it into my template";
      if (!data.ok) { setResult(note, data.message, false); return; }
      setResult(note, "");
      open(data.template);
      setResult(result, data.message, true);
      showProblems(data.problems);
      editor.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }
  const blankBtn = $("own-blank");
  if (blankBtn) blankBtn.addEventListener("click", () => open(DATA.blank));
  const redo = $("own-redo");
  if (redo) redo.addEventListener("click", () => { start.hidden = false; $("own-example").focus(); });

  // ── Preview and save ─────────────────────────────────────────────────────
  $("own-preview-btn").addEventListener("click", async () => {
    const data = await postJSON("/api/own-template/preview", { template: state });
    showProblems(data.problems);
    if (!data.ok) { setResult(result, data.message, false); return; }
    setResult(result, "");
    showPreview(data);
  });
  $("own-save-btn").addEventListener("click", async (e) => {
    const button = e.currentTarget;
    button.disabled = true;
    const data = await postJSON("/api/own-template/save", { template: state, use: $("own-use").checked });
    button.disabled = false;
    showProblems(data.problems);
    setResult(result, data.message, !!data.ok);
    if (data.preview) showPreview(data);
    if (data.ok) setTimeout(() => window.location.reload(), 1600);
  });

  if (DATA.template) open(DATA.template);
})();
