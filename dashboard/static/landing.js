// Landing page motion: reveal sections as they scroll in, count the tracker
// numbers up, and play the style switcher. Transform/opacity only, one
// IntersectionObserver, nothing at all when the visitor prefers less motion.
(() => {
  "use strict";
  const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ── Style switcher demo: the same company, four styles ───────────────────
  const tabs = document.querySelector("[data-style-tabs]");
  const field = (name) => document.querySelector(`[data-demo="${name}"]`);
  const STYLES = [
    { subject: "End-of-Study Internship — Security Operations", greeting: "Dear Northwind Team,",
      body: 'What interests me most about Northwind is your work on <mark>managed detection and response for mid-sized banks</mark>.' },
    { subject: "Internship (Security Operations) — Sam Doe", greeting: "Dear Northwind Team,",
      body: 'Your work on <mark>managed detection and response for mid-sized banks</mark> is exactly why I\'m writing.' },
    { subject: "Quick call about security operations?", greeting: "Dear Northwind Team,",
      body: 'Would you have 15 minutes for a short call? I\'d love to hear how your team runs <mark>detection and response for banks</mark>.' },
    { subject: "Internship from March 2027 — Security Operations", greeting: "Dear Northwind Team,",
      body: 'I\'m writing with a spontaneous application: a six-month internship from March 2027, in your <mark>detection and response</mark> team.' },
  ];
  let current = 0;
  let timer = null;
  function show(index) {
    if (!tabs) return;
    current = index;
    tabs.querySelectorAll("[data-style]").forEach((tab) => tab.classList.toggle("is-on", Number(tab.dataset.style) === index));
    const mail = tabs.nextElementSibling;
    mail.classList.remove("is-swapping");
    void mail.offsetWidth; // restart the fade
    mail.classList.add("is-swapping");
    field("subject").textContent = STYLES[index].subject;
    field("greeting").textContent = STYLES[index].greeting;
    field("body").innerHTML = STYLES[index].body;  // fixed strings above, never user input
  }
  function play() {
    if (calm || timer) return;
    timer = setInterval(() => show((current + 1) % STYLES.length), 3200);
  }
  function pause() { clearInterval(timer); timer = null; }
  if (tabs) {
    tabs.addEventListener("click", (e) => {
      const tab = e.target.closest("[data-style]");
      if (!tab) return;
      pause();
      show(Number(tab.dataset.style));
    });
    tabs.querySelectorAll("[data-style]").forEach((tab) => tab.setAttribute("role", "button"));
  }

  // ── Counting the tracker numbers up ──────────────────────────────────────
  function countUp(el) {
    const target = Number(el.dataset.count);
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / 1100);
      el.textContent = Math.round(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  if (calm || !("IntersectionObserver" in window)) return;

  // Only what is still below the fold is hidden, so nothing on screen at
  // load ever flickers.
  const fold = window.innerHeight;
  const watched = [];
  document.querySelectorAll("[data-reveal]").forEach((el) => {
    if (el.getBoundingClientRect().top > fold * 0.9) {
      el.classList.add("will-reveal");
      watched.push(el);
    }
  });
  const counters = Array.from(document.querySelectorAll("[data-count]"));

  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      const el = entry.target;
      if (el.matches("[data-style-tabs]")) {
        entry.isIntersecting ? play() : pause();   // only animate while visible
        return;
      }
      if (!entry.isIntersecting) return;
      if (el.dataset.count) countUp(el);
      else el.classList.add("is-in");
      observer.unobserve(el);
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.12 });

  watched.forEach((el) => observer.observe(el));
  counters.forEach((el) => observer.observe(el));
  if (tabs) observer.observe(tabs);
})();
