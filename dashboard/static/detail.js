/* Detail page: Preview/Edit tabs, live word count, send, skip, regenerate. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    return response.json();
  }

  function setResult(text, ok) {
    const el = $("detail-action-result");
    if (!el) return;
    el.textContent = text;
    el.style.color = ok === undefined ? "" : (ok ? "var(--st-sent)" : "var(--st-failed)");
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

  // ── Live word count + preview sync while editing ──────────────────────────
  const bodyField = $("body");
  const subjectField = $("subject");
  const wordCountEl = $("word-count");
  function syncFromEditor() {
    if (bodyField) {
      const words = bodyField.value.trim().split(/\s+/).filter(Boolean).length;
      if (wordCountEl) {
        wordCountEl.textContent = `${words} words (target 200–320)`;
        wordCountEl.classList.toggle("word-count--warn", words < 180 || words > 360);
      }
      const previewBody = $("preview-body");
      if (previewBody) previewBody.textContent = bodyField.value;
    }
    if (subjectField) {
      const previewSubject = $("preview-subject");
      if (previewSubject) previewSubject.textContent = subjectField.value || "—";
    }
  }
  if (bodyField) bodyField.addEventListener("input", syncFromEditor);
  if (subjectField) subjectField.addEventListener("input", syncFromEditor);

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
        if (e.key === "Enter") done(true);
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
