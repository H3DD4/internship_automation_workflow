/* Detail page: Preview/Edit tabs, live word count, send, skip, regenerate. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

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

  function setResult(text, ok) {
    const el = $("detail-action-result");
    if (!el) return;
    el.textContent = text;
    el.style.color = ok === undefined ? "" : (ok ? "var(--st-sent)" : "var(--st-failed)");
  }

  // ── Re-scan this company ─────────────────────────────────────────────────
  const rescanBtn = $("rescan-detail-btn");
  if (rescanBtn) {
    rescanBtn.addEventListener("click", async () => {
      if (!window.confirm("Re-scan this company?\n\nIts website is read again and a new draft is written " +
          "with your AI key. Your chosen style is kept; any edits you made to this draft are lost.\n\n" +
          "Only needed when the research went wrong — to change the style, just pick one under Style.")) return;
      rescanBtn.disabled = true;
      setResult("Starting…");
      const data = await postJSON("/api/rescan", { app_ids: [+rescanBtn.dataset.appId] });
      setResult(data.message, !!data.ok);
      if (data.ok) setTimeout(() => window.location.reload(), 1200);
      else rescanBtn.disabled = false;
    });
  }

  // ── Preview / Edit tabs ──────────────────────────────────────────────────
  const tabs = Array.from(document.querySelectorAll(".detail-tab"));
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      tabs.forEach((t) => t.classList.toggle("detail-tab--active", t === tab));
      ["preview", "edit"].forEach((name) => {
        const panel = $(`panel-${name}`);
        if (panel) panel.hidden = name !== tab.dataset.tab;
      });
    });
  });

  // ── Editing: live preview, word count, and saving in place ────────────────
  const bodyField = $("body");
  const subjectField = $("subject");
  const wordCountEl = $("word-count");
  const editor = $("email-editor");
  const saveBtn = $("save-draft-btn");
  const revertBtn = $("revert-draft-btn");
  const saveState = $("save-state");
  const previewDirty = $("preview-dirty");

  // What is currently stored server-side. Updated on every successful save, so
  // "dirty" always means "differs from the saved draft" rather than "differs
  // from whatever the page loaded with".
  let saved = {
    subject: subjectField ? subjectField.value : "",
    body: bodyField ? bodyField.value : "",
  };

  const current = () => ({
    subject: subjectField ? subjectField.value : "",
    body: bodyField ? bodyField.value : "",
  });
  const isDirty = () => {
    const now = current();
    return now.subject !== saved.subject || now.body !== saved.body;
  };

  function setSaveState(text, kind) {
    if (!saveState) return;
    saveState.textContent = text;
    saveState.className = "save-state" + (kind ? ` save-state--${kind}` : "");
  }

  function syncFromEditor() {
    if (bodyField) {
      const words = bodyField.value.trim().split(/\s+/).filter(Boolean).length;
      if (wordCountEl) {
        wordCountEl.textContent = `${words} words · most replies at 50–125`;
        wordCountEl.classList.toggle("word-count--warn", words < 50 || words > 200);
      }
      const previewBody = $("preview-body");
      if (previewBody) previewBody.textContent = bodyField.value;
    }
    if (subjectField) {
      const previewSubject = $("preview-subject");
      if (previewSubject) previewSubject.textContent = subjectField.value || "—";
    }
    const dirty = isDirty();
    if (previewDirty) previewDirty.hidden = !dirty;
    if (revertBtn) revertBtn.hidden = !dirty;
    if (dirty) setSaveState("Unsaved changes", "dirty");
    else setSaveState("");
  }
  if (bodyField) bodyField.addEventListener("input", syncFromEditor);
  if (subjectField) subjectField.addEventListener("input", syncFromEditor);

  async function saveDraft() {
    if (!editor || !saveBtn) return;
    const payload = current();
    if (!payload.subject.trim() || !payload.body.trim()) {
      setSaveState("Subject and message can't be empty", "bad");
      return;
    }
    saveBtn.disabled = true;
    setSaveState("Saving…");
    try {
      const data = await postJSON("/api/update-draft", {
        app_id: parseInt(editor.dataset.appId, 10),
        subject: payload.subject,
        body: payload.body,
      });
      if (!data.ok) {
        setSaveState(`✗ ${data.message}`, "bad");
        return;
      }
      saved = payload;
      syncFromEditor();
      setSaveState("Saved ✓", "ok");
      // Saving an edit also moves a failed draft back to "ready", so the
      // status pill and the Send button on this page would be stale.
      if (data.status && data.status !== editor.dataset.status) {
        setSaveState("Saved ✓ — refreshing", "ok");
        setTimeout(() => window.location.reload(), 700);
      }
    } catch (err) {
      setSaveState("✗ Could not reach the dashboard — your text is still here", "bad");
    } finally {
      saveBtn.disabled = false;
    }
  }

  if (editor) {
    editor.addEventListener("submit", (event) => {
      event.preventDefault();
      saveDraft();
    });
  }
  if (revertBtn) {
    revertBtn.addEventListener("click", () => {
      if (subjectField) subjectField.value = saved.subject;
      if (bodyField) bodyField.value = saved.body;
      syncFromEditor();
      setSaveState("Reverted to the saved draft", "ok");
    });
  }
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && editor) {
      event.preventDefault();
      saveDraft();
    }
  });
  // Closing the tab mid-edit is the one way to lose work that no amount of
  // in-page state would recover.
  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) event.preventDefault();
  });

  // ── Confirm modal ────────────────────────────────────────────────────────
  function confirmSend(company, email) {
    const modal = $("confirm-modal");
    if (!modal) return Promise.resolve(true);
    $("confirm-copy").textContent =
      "This sends the email below from your Gmail, with your CV attached. It can't be undone.";
    const list = $("confirm-list");
    list.innerHTML = "";
    const li = document.createElement("li");
    li.textContent = `${company} · ${email}`;
    list.appendChild(li);

    // The send job reads the stored draft, so anything typed and not saved is
    // not what goes out. Warn instead of quietly sending the older text.
    const warn = $("confirm-warn");
    if (warn) {
      const dirty = isDirty();
      warn.hidden = !dirty;
      warn.textContent = dirty
        ? "You have unsaved edits. This sends the last SAVED version — cancel and save first if you meant to send your changes."
        : "";
    }

    modal.hidden = false;
    $("confirm-ok").focus();

    return new Promise((resolve) => {
      const done = (result) => {
        modal.hidden = true;
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
      modal.onclick = (e) => { if (e.target === modal) done(false); };
      document.addEventListener("keydown", onKey);
    });
  }

  // ── Send ─────────────────────────────────────────────────────────────────
  const sendBtn = $("send-detail-btn");
  if (sendBtn) {
    const company = document.querySelector("h1").textContent.trim();
    const email = document.querySelector(".subtitle").textContent.trim().split(" ")[0];
    sendBtn.addEventListener("click", async () => {
      if (!(await confirmSend(company, email))) return;
      sendBtn.disabled = true;
      sendBtn.textContent = "Sending…";
      setResult("");
      try {
        const data = await postJSON("/api/send-job", { app_ids: [parseInt(sendBtn.dataset.appId, 10)] });
        if (!data.ok) {
          setResult(`✗ ${data.message}`, false);
          sendBtn.disabled = false;
          sendBtn.textContent = "Send now";
          return;
        }
        const poll = async () => {
          const response = await fetch(`/api/send-job/${data.job_id}`);
          const job = await response.json();
          if (job.status === "completed") {
            const item = (job.items || [])[0] || {};
            if (item.status === "sent") {
              setResult("✓ Sent", true);
              sendBtn.textContent = "Sent ✓";
              setTimeout(() => window.location.reload(), 1200);
            } else {
              setResult(`✗ ${item.error || "Send failed"}`, false);
              sendBtn.disabled = false;
              sendBtn.textContent = "Send now";
            }
            return;
          }
          setResult("Sending…");
          setTimeout(poll, 1500);
        };
        poll();
      } catch (err) {
        setResult(`✗ ${err.message}`, false);
        sendBtn.disabled = false;
        sendBtn.textContent = "Send now";
      }
    });
  }

  // ── Regenerate the draft (reuses saved research) ──────────────────────────
  const regenBtn = $("regenerate-btn");
  if (regenBtn) {
    regenBtn.addEventListener("click", async () => {
      regenBtn.disabled = true;
      const original = regenBtn.textContent;
      regenBtn.textContent = "Rebuilding…";
      setResult("");
      try {
        const data = await postJSON(`/api/regenerate/${regenBtn.dataset.appId}`, {});
        if (!data.ok) {
          setResult(`✗ ${data.message}`, false);
          return;
        }
        setResult("✓ Draft rebuilt", true);
        setTimeout(() => window.location.reload(), 900);
      } catch (err) {
        setResult(`✗ ${err.message}`, false);
      } finally {
        regenBtn.disabled = false;
        regenBtn.textContent = original;
      }
    });
  }

  // ── After a style switch: highlight the sentences the new style changed ──
  (function highlightStyleChanges() {
    const select = $("style-select");
    const preview = $("preview-body");
    if (!select || !preview) return;
    let saved = null;
    try {
      const key = `style-diff-${select.dataset.appId}`;
      saved = JSON.parse(sessionStorage.getItem(key) || "null");
      sessionStorage.removeItem(key);
    } catch (_) { return; }
    if (!saved || !saved.body) return;
    const split = (text) => text.split(/(?<=[.!?:])\s+|\n+/).map((s) => s.trim()).filter(Boolean);
    const before = new Set(split(saved.body));
    const text = preview.textContent;
    let changed = 0;
    // Rebuild the preview as text nodes and <mark>s — never as HTML from the draft.
    const parts = text.split(/((?<=[.!?:])\s+|\n+)/);
    preview.textContent = "";
    parts.forEach((part) => {
      const sentence = part.trim();
      if (sentence && !/^\s+$/.test(part) && !before.has(sentence)) {
        const mark = document.createElement("mark");
        mark.className = "style-changed";
        mark.textContent = part;
        preview.appendChild(mark);
        changed += 1;
      } else {
        preview.appendChild(document.createTextNode(part));
      }
    });
    if (changed) {
      setResult(`✓ Rewritten in “${saved.name}” — the highlighted sentences are what changed.`, true);
    } else {
      setResult(`“${saved.name}” produced the same text as before for this company.`);
    }
  })();

  // ── Style switch ─────────────────────────────────────────────────────────
  // Picking a style shows what it's for; confirming rewrites this email in it
  // from the same research and approved facts (instant, no AI).
  const styleSelect = $("style-select");
  if (styleSelect) {
    const showNote = (id) => document.querySelectorAll("[data-style-note]").forEach((n) => {
      n.hidden = n.dataset.styleNote !== id;
    });
    styleSelect.addEventListener("change", async () => {
      const style = styleSelect.value;
      const name = styleSelect.options[styleSelect.selectedIndex].text;
      showNote(style);
      if (!window.confirm(`Rewrite this email in the “${name}” style? Any edits you made by hand to this draft will be replaced.`)) {
        styleSelect.value = styleSelect.dataset.current;
        showNote(styleSelect.dataset.current);
        return;
      }
      styleSelect.disabled = true;
      setResult(`Rewriting in “${name}”…`);
      try {
        const data = await postJSON(`/api/style/${styleSelect.dataset.appId}`, { style });
        if (!data.ok) {
          setResult(`✗ ${data.message}`, false);
          styleSelect.value = styleSelect.dataset.current;
          showNote(styleSelect.dataset.current);
          return;
        }
        setResult(`✓ Rewritten in “${name}”`, true);
        // Remember the old text so the reloaded page can highlight what changed.
        try {
          const preview = $("preview-body");
          sessionStorage.setItem(`style-diff-${styleSelect.dataset.appId}`,
            JSON.stringify({ body: preview ? preview.textContent : "", name }));
        } catch (_) { /* storage unavailable: no highlight, nothing else lost */ }
        setTimeout(() => window.location.reload(), 500);
      } catch (err) {
        setResult(`✗ ${err.message}`, false);
      } finally {
        styleSelect.disabled = false;
      }
    });
  }

  // ── EN | FR switch ───────────────────────────────────────────────────────
  // Rewrites this company's email in the other language from the approved
  // wording (instant, no AI). A hand edit would be replaced, so ask first.
  const langSwitch = $("lang-switch");
  if (langSwitch) {
    langSwitch.addEventListener("click", async (event) => {
      const option = event.target.closest(".lang-switch__option");
      if (!option || option.classList.contains("lang-switch__option--active")) return;
      if (!langSwitch.dataset.editable) {
        setResult("✗ This email can't be changed any more.", false);
        return;
      }
      const lang = option.dataset.lang;
      const label = lang === "fr" ? "French" : "English";
      if (!window.confirm(`Rewrite this email in ${label}? Any edits you made by hand to this draft will be replaced.`)) return;
      langSwitch.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      setResult(`Rewriting in ${label}…`);
      try {
        const data = await postJSON(`/api/language/${langSwitch.dataset.appId}`, { language: lang });
        if (!data.ok) {
          setResult(`✗ ${data.message}`, false);
          return;
        }
        setResult(`✓ Now in ${label}`, true);
        setTimeout(() => window.location.reload(), 500);
      } catch (err) {
        setResult(`✗ ${err.message}`, false);
      } finally {
        langSwitch.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    });
  }

  // ── Favorite ─────────────────────────────────────────────────────────────
  const favBtn = $("favorite-detail-btn");
  if (favBtn) {
    const paint = (on) => {
      favBtn.classList.toggle("fav-toggle--on", on);
      favBtn.setAttribute("aria-pressed", on ? "true" : "false");
      favBtn.textContent = on ? "★ Favorite" : "☆ Add to favorites";
    };
    favBtn.addEventListener("click", async () => {
      const on = favBtn.getAttribute("aria-pressed") !== "true";
      paint(on);
      try {
        const data = await postJSON("/api/favorite", { app_ids: [+favBtn.dataset.appId], favorite: on });
        if (!data.ok) throw new Error(data.message);
      } catch (err) {
        paint(!on);
        setResult(`✗ ${err.message}`, false);
      }
    });
  }

  // ── Skip / un-skip ───────────────────────────────────────────────────────
  [["skip-detail-btn", false], ["unskip-detail-btn", true]].forEach(([id, unskip]) => {
    const btn = $(id);
    if (!btn) return;
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const data = await postJSON(`/api/skip/${btn.dataset.appId}`, { unskip });
      if (!data.ok) {
        setResult(`✗ ${data.message}`, false);
        btn.disabled = false;
        return;
      }
      window.location.reload();
    });
  });
})();
