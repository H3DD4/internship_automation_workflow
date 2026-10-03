"""CV check: how an applicant-tracking system (ATS) will read this CV.

Rules only — no AI. Every finding is computed from the file itself and quotes
what it found, so the same CV always gets the same report and nothing is a
guess. The rules are the ones ATS vendors and career guides agree on (Jobscan,
Indeed, university career centres): machine-readable text, one column,
standard section headings, contact details in the body, standard bullets,
dates, measurable results.

  inspect_file(filename, content) -> (lines, meta)   what a parser sees
  evaluate(lines, meta) -> report                    the score and the fixes
  check(filename, content) -> report                 both

An ATS score predicts parsing, not hiring: it can't promise an interview.
"""

from __future__ import annotations

import io
import re
import zipfile

MAX_LINES_SHOWN = 220

# ---------------------------------------------------------------------------
# What a parser sees
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+|00)?\d[\d\s().-]{7,16}\d(?!\d)")
_DATEISH_RE = re.compile(r"^[\s\d/.\-–—|:,()]*(?:[A-Za-zéûÉ]{3,9}\.?\s*)?(?:19|20)\d{2}.{0,22}$")


def _clean(line: str) -> str:
    return re.sub(r"[ \t ]+", " ", line or "").strip()


def _inspect_pdf(content: bytes) -> tuple:
    from pypdf import PdfReader
    meta = {"kind": "pdf", "pages": 0, "images": 0, "two_sided_rows": 0, "rows": 0, "side_example": ""}
    lines = []
    try:
        reader = PdfReader(io.BytesIO(content))
        meta["pages"] = len(reader.pages)
        for page in reader.pages[:6]:
            width = float(page.mediabox.width or 595)
            chunks = []

            def visit(text, cm, tm, font, size, chunks=chunks):
                if text and text.strip():
                    chunks.append((round(float(tm[5])), float(tm[4]), text.strip()))

            try:
                text = page.extract_text(visitor_text=visit) or ""
            except Exception:
                text = page.extract_text() or ""
            lines += [_clean(line) for line in text.splitlines() if _clean(line)]
            # Rows with real text on both halves of the page = columns or a
            # table. A short date on the right of a job title doesn't count.
            rows = {}
            for y, x, text_chunk in chunks:
                rows.setdefault(y, []).append((x, text_chunk))
            for row in rows.values():
                left = [t for x, t in row if x <= width * 0.30]
                right = [t for x, t in row if x >= width * 0.48 and len(t) > 3
                         and not _DATEISH_RE.match(t)]
                meta["rows"] += 1
                if left and right:
                    meta["two_sided_rows"] += 1
                    meta["side_example"] = meta["side_example"] or f"{left[0][:40]}  |  {right[0][:40]}"
            try:
                objects = (page.get("/Resources") or {}).get("/XObject") or {}
                meta["images"] += sum(1 for o in objects.values()
                                      if (o.get_object().get("/Subtype") == "/Image"))
            except Exception:
                pass
    except Exception:
        meta["unreadable"] = True
    return lines, meta


def _inspect_docx(content: bytes) -> tuple:
    import defusedxml.ElementTree as SafeET
    meta = {"kind": "docx", "pages": 0, "images": 0, "tables": 0, "text_boxes": 0,
            "header_footer_text": ""}
    lines = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            body = archive.read("word/document.xml")
            if len(body) > 6 * 1024 * 1024:
                raise ValueError("too large")
            root = SafeET.fromstring(body)
            for paragraph in root.iter(f"{_W}p"):
                text = _clean("".join(node.text or "" for node in paragraph.iter(f"{_W}t")))
                if text:
                    lines.append(text)
            meta["tables"] = sum(1 for _ in root.iter(f"{_W}tbl"))
            meta["text_boxes"] = sum(1 for _ in root.iter(f"{_W}txbxContent"))
            meta["images"] = sum(1 for n in names if n.startswith("word/media/"))
            parts = []
            for name in names:
                if re.match(r"word/(header|footer)\d*\.xml$", name):
                    part = SafeET.fromstring(archive.read(name))
                    parts.append(" ".join(node.text or "" for node in part.iter(f"{_W}t")))
            meta["header_footer_text"] = _clean(" ".join(parts))
    except Exception:
        meta["unreadable"] = True
    return lines, meta


def inspect_file(filename: str, content: bytes) -> tuple:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        lines, meta = _inspect_pdf(content)
    elif name.endswith(".docx"):
        lines, meta = _inspect_docx(content)
    else:
        lines, meta = [], {"kind": name.rsplit(".", 1)[-1] if "." in name else "?", "unsupported": True}
    meta["filename"] = filename or ""
    meta["bytes"] = len(content or b"")
    return lines, meta


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

SECTIONS = {
    "experience": ("experience", "experiences", "work experience", "professional experience",
                   "employment", "work history", "internships", "internship experience",
                   "expérience", "expériences", "expérience professionnelle",
                   "expériences professionnelles", "parcours professionnel", "stages"),
    "education": ("education", "academic background", "studies", "formation", "formations",
                  "éducation", "études", "parcours académique", "diplômes", "cursus"),
    "skills": ("skills", "technical skills", "core skills", "key skills", "competencies",
               "compétences", "compétences techniques", "compétences clés", "savoir-faire",
               "technologies", "outils"),
    "projects": ("projects", "academic projects", "personal projects", "projets",
                 "projets académiques", "projets personnels", "réalisations"),
    "languages": ("languages", "langues", "language skills"),
    # Known headings that only mark where another section ends.
    "certifications": ("certifications", "certificates", "licenses & certifications", "certificats",
                       "formations complémentaires"),
    "awards": ("awards", "achievements", "honors", "honours", "awards & achievements",
               "distinctions", "prix", "récompenses", "prix et distinctions"),
    "summary": ("summary", "profile", "about me", "objective", "professional summary",
                "profil", "à propos", "résumé", "objectif"),
    "activities": ("activities", "extracurricular activities", "volunteering", "leadership",
                   "interests", "hobbies", "activités", "vie associative", "bénévolat",
                   "centres d'intérêt", "loisirs"),
}
REQUIRED = ("experience", "education", "skills")
SECTION_NAMES = {"experience": "Experience", "education": "Education", "skills": "Skills",
                 "projects": "Projects", "languages": "Languages"}

STANDARD_BULLETS = "•-–*·◦▪●○"
ODD_BULLETS = "➢➤►▶✓✔✗➔→⇒❖◆◇■□★☆♦➜➣✦»"
ACTION_VERBS = {
    # English
    "achieved", "analysed", "analyzed", "automated", "built", "collaborated", "conducted", "created",
    "delivered", "deployed", "designed", "developed", "drove", "engineered", "established", "evaluated",
    "implemented", "improved", "increased", "integrated", "launched", "led", "maintained", "managed",
    "migrated", "optimised", "optimized", "organised", "organized", "performed", "planned", "presented",
    "produced", "reduced", "researched", "resolved", "reviewed", "secured", "tested", "trained", "wrote",
    "audited", "configured", "coordinated", "documented", "identified", "monitored", "negotiated",
    "prepared", "supported", "taught", "won", "contributed", "modelled", "modeled", "assessed",
    "reported", "shipped", "founded", "published", "mentored", "ran", "set", "solved", "scaled",
    "refactored", "investigated", "detected", "simulated", "validated", "translated", "handled",
    "drafted", "explored", "extracted", "generated", "used", "applied", "ranked",
    # French (past participles and nominal openers both common on French CVs)
    "analysé", "automatisé", "conçu", "créé", "déployé", "développé", "dirigé", "géré", "implémenté",
    "amélioré", "intégré", "lancé", "mené", "optimisé", "organisé", "piloté", "présenté", "réalisé",
    "réduit", "rédigé", "testé", "formé", "audité", "configuré", "coordonné", "documenté", "identifié",
    "participé", "contribué", "mis", "assuré", "élaboré", "encadré", "animé", "construit", "obtenu",
    "conception", "développement", "réalisation", "mise", "gestion", "analyse", "création",
    "déploiement", "optimisation", "rédaction", "participation", "implémentation", "automatisation",
}
_PRONOUN_RE = re.compile(r"(?<![\w'’])(I|my|me|je|j['’]|mon|ma|mes)(?![\w])", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)?\s?(?:%|k\b|K\b|M\b|€|\$|x\b|\+)?")
_LINK_RE = re.compile(r"linkedin\.com|github\.com|gitlab\.com|behance\.net|portfolio|https?://", re.IGNORECASE)
_GARBAGE_RE = re.compile(r"\(cid:\d+\)|�|[-]")


def _heading(line: str) -> str | None:
    """The standard section this line announces, if it is a heading."""
    text = re.sub(r"[:\-–—|•\s]+$", "", line).strip(" :-–—|•").lower()
    if not text or len(text) > 42 or len(text.split()) > 5:
        return None
    for section, names in SECTIONS.items():
        if text in names:
            return section
    return None


def _bullet(line: str) -> str:
    return line[0] if line and line[0] in STANDARD_BULLETS + ODD_BULLETS else ""


def _measurable(line: str) -> bool:
    """A number that measures something — not a year, a phone or a date."""
    rest = _YEAR_RE.sub(" ", _PHONE_RE.sub(" ", line))
    rest = re.sub(r"\b\d{1,2}[/.-]\d{1,2}\b", " ", rest)
    # Not measurements: "3D", "Bac+5", version numbers.
    rest = re.sub(r"\b\d[dD]\b|\b[A-Za-z]+\+\d\b|\b[vV]?\d+\.\d+(?:\.\d+)?\b", " ", rest)
    return bool(_NUMBER_RE.search(rest))


def _check(checks, id_, category, points, status, title, detail, fix=""):
    earned = {"ok": points, "warn": points / 2, "fail": 0}[status]
    checks.append({"id": id_, "category": category, "points": points, "earned": earned,
                   "status": status, "title": title, "detail": detail, "fix": fix if status != "ok" else ""})


def evaluate(lines: list, meta: dict) -> dict:
    lines = [_clean(l) for l in lines if _clean(l)]
    text = "\n".join(lines)
    words = len(text.split())
    checks, marks = [], {}

    def mark(index, severity, why):
        if index not in marks or severity == "fail":
            marks[index] = {"severity": severity, "why": why}

    # ---- Can a machine read it? -------------------------------------------
    kind = meta.get("kind")
    if meta.get("unsupported"):
        _check(checks, "file_type", "Readable by machines", 5, "fail", "File type",
               f"“.{kind}” files aren't read by most systems.", "Save your CV as a PDF or a .docx file.")
    else:
        _check(checks, "file_type", "Readable by machines", 5, "ok", "File type",
               f"{'PDF' if kind == 'pdf' else 'Word (.docx)'} — a format every system accepts.")

    chars = len(text)
    if meta.get("unreadable") or chars < 200:
        _check(checks, "text", "Readable by machines", 15, "fail", "Text a machine can read",
               f"Only {chars} characters of text could be read"
               + (f" — and the file has {meta['images']} image(s): this looks like a scan or a design exported as a picture." if meta.get("images") else "."),
               "Export the CV from Word, Google Docs or your editor as a text PDF (“Save as PDF”), never as a scan or a photo.")
    elif chars < 800:
        _check(checks, "text", "Readable by machines", 15, "warn", "Text a machine can read",
               f"Only {chars} characters could be read — part of your CV may be inside images or text boxes.",
               "Make sure every section is real text you can select with the mouse.")
    else:
        _check(checks, "text", "Readable by machines", 15, "ok", "Text a machine can read",
               f"{words} words read without trouble.")

    garbage = [i for i, l in enumerate(lines) if _GARBAGE_RE.search(l)]
    if garbage:
        for i in garbage[:12]:
            mark(i, "fail" if len(garbage) > 3 else "warn", "Characters a machine can't read")
        _check(checks, "encoding", "Readable by machines", 5, "fail" if len(garbage) > 3 else "warn",
               "Fonts and symbols", f"{len(garbage)} line(s) contain characters that come out unreadable, e.g. “{lines[garbage[0]][:60]}”.",
               "Use a standard font (Arial, Calibri, Helvetica, Times) and replace icon fonts with plain words.")
    else:
        _check(checks, "encoding", "Readable by machines", 5, "ok", "Fonts and symbols",
               "Every character is readable.")

    if kind == "docx":
        tables, boxes = meta.get("tables", 0), meta.get("text_boxes", 0)
        if tables or boxes:
            found = " and ".join(p for p in (f"{tables} table(s)" if tables else "", f"{boxes} text box(es)" if boxes else "") if p)
            _check(checks, "layout", "Readable by machines", 8, "fail" if boxes else "warn", "One simple column",
                   f"The file contains {found}. Systems read tables cell by cell in the wrong order and often skip text boxes.",
                   "Rebuild the layout as one column of normal paragraphs — no tables, no text boxes.")
        else:
            _check(checks, "layout", "Readable by machines", 8, "ok", "One simple column",
                   "No tables or text boxes.")
    else:
        rows, sided = meta.get("rows", 0), meta.get("two_sided_rows", 0)
        share = sided / rows if rows else 0
        if sided >= 8 and share >= 0.25:
            _check(checks, "layout", "Readable by machines", 8, "fail" if share >= 0.45 else "warn", "One simple column",
                   f"{sided} of {rows} lines have text on both sides of the page (e.g. “{meta.get('side_example', '')}”) — "
                   "a two-column or table layout. Systems read straight across and mix the two columns together.",
                   "Use a single-column layout: one section under the other, across the full width.")
        else:
            _check(checks, "layout", "Readable by machines", 8, "ok", "One simple column",
                   "The text reads top to bottom in one column.")

    emails = _EMAIL_RE.findall(text)
    hidden = _EMAIL_RE.findall(meta.get("header_footer_text", ""))
    if emails:
        _check(checks, "email", "Readable by machines", 4, "ok", "Email address", f"Found: {emails[0]}.")
    elif hidden:
        _check(checks, "email", "Readable by machines", 4, "fail", "Email address",
               f"{hidden[0]} is only in the page header or footer, which most systems skip.",
               "Move your contact details into the body of the page, at the top.")
    else:
        _check(checks, "email", "Readable by machines", 4, "fail", "Email address",
               "No email address could be read" + (" — it may be inside an image or an icon." if meta.get("images") else "."),
               "Write your email as plain text at the top of the CV.")
    phone = next((m.group(0) for l in lines[:25] for m in [_PHONE_RE.search(l)] if m and not _YEAR_RE.fullmatch(m.group(0).strip())), "")
    if phone:
        _check(checks, "phone", "Readable by machines", 3, "ok", "Phone number", f"Found: {phone.strip()}.")
    else:
        _check(checks, "phone", "Readable by machines", 3, "warn", "Phone number",
               "No phone number was found near the top.", "Add your phone number with its country code, as plain text.")

    # ---- Structure ---------------------------------------------------------
    found = {}
    for i, line in enumerate(lines):
        section = _heading(line)
        if section and section not in found:
            found[section] = i
    missing = [s for s in REQUIRED if s not in found]
    # Which section each line sits in ("" before the first heading).
    where, current = [], ""
    for line in lines:
        current = _heading(line) or current
        where.append(current)
    doing = {"experience", "projects"} if any(w in ("experience", "projects") for w in where) else {""}
    for section in REQUIRED:
        if section in found:
            _check(checks, f"section_{section}", "Structure", 5, "ok", f"“{SECTION_NAMES[section]}” section",
                   f"Found the heading “{lines[found[section]]}”.")
        else:
            examples = " / ".join(n.title() for n in SECTIONS[section][:2]) + " / " + SECTIONS[section][-4].title()
            _check(checks, f"section_{section}", "Structure", 5, "fail", f"“{SECTION_NAMES[section]}” section",
                   "No heading a system recognises was found for it.",
                   f"Add a line with a standard heading on its own, such as: {examples}.")

    years = _YEAR_RE.findall(text)
    if len(years) >= 2:
        _check(checks, "dates", "Structure", 5, "ok", "Dates", f"{len(years)} dates found, so each experience can be placed in time.")
    else:
        _check(checks, "dates", "Structure", 5, "fail" if not years else "warn", "Dates",
               f"Only {len(years)} year(s) found.", "Give start and end dates for every experience and degree, e.g. “Sep 2024 – Jun 2025”.")

    pages = meta.get("pages") or 0
    if words < 150:
        _check(checks, "length", "Structure", 5, "fail", "Length", f"{words} words — too little for a system to score.",
               "Describe each experience and project in 2–4 bullet points.")
    elif pages >= 3 or words > 1000:
        _check(checks, "length", "Structure", 5, "warn", "Length",
               f"{pages or '?'} pages, {words} words — long for a student CV.", "Aim for one page (two at most): keep the most recent and relevant items.")
    elif words < 250:
        _check(checks, "length", "Structure", 5, "warn", "Length", f"{words} words — a little thin.",
               "Add detail to your experience and projects: what you did, with what, and the result.")
    else:
        _check(checks, "length", "Structure", 5, "ok", "Length", f"{pages or 1} page(s), {words} words.")

    bullets = [i for i, l in enumerate(lines) if _bullet(l)]
    odd = [i for i in bullets if lines[i][0] in ODD_BULLETS]
    if odd:
        for i in odd[:15]:
            mark(i, "warn", "Decorative bullet — may be read as a stray character")
        _check(checks, "bullets", "Structure", 5, "warn", "Bullet points",
               f"{len(odd)} line(s) start with a decorative symbol (“{lines[odd[0]][0]}”), which some systems turn into a stray character.",
               "Use plain round bullets (•) or dashes (-).")
    elif len(bullets) >= 4:
        _check(checks, "bullets", "Structure", 5, "ok", "Bullet points", f"{len(bullets)} bullet points with standard symbols.")
    else:
        _check(checks, "bullets", "Structure", 5, "warn", "Bullet points",
               f"Only {len(bullets)} bullet point(s) found.", "Describe each experience with short bullet points rather than paragraphs.")

    if _LINK_RE.search(text):
        _check(checks, "links", "Structure", 3, "ok", "Profile link", "A LinkedIn, GitHub or portfolio link is present.")
    else:
        _check(checks, "links", "Structure", 3, "warn", "Profile link", "No LinkedIn, GitHub or portfolio link found.",
               "Add your LinkedIn (and GitHub or portfolio if you have one) as a full address.")

    # ---- Content -----------------------------------------------------------
    # Figures count where the work is described: experience and projects.
    measured = [i for i, l in enumerate(lines) if where[i] in doing and _heading(l) is None and _measurable(l)]
    if len(measured) >= 3:
        _check(checks, "numbers", "Content", 10, "ok", "Results with numbers",
               f"{len(measured)} lines carry a figure, e.g. “{lines[measured[0]][:80]}”.")
    else:
        for i in [b for b in bullets if where[b] in doing and b not in measured][:8]:
            mark(i, "warn", "No figure — add how much, how many or how fast")
        _check(checks, "numbers", "Content", 10, "fail" if not measured else "warn", "Results with numbers",
               f"Only {len(measured)} line(s) give a figure.",
               "Add numbers wherever true: how many users, files, tests, people; a percentage; a time saved; a grade or rank.")

    def first_word(line):
        return re.sub(r"^[^\wÀ-ÿ]+", "", line).split(" ")[0].lower().strip(",.:;")
    work = [i for i in bullets if where[i] in doing]      # not the skills or languages lists
    started = [i for i in work if first_word(lines[i]) in ACTION_VERBS]
    if work and len(started) / len(work) >= 0.4:
        _check(checks, "verbs", "Content", 6, "ok", "Action verbs",
               f"{len(started)} of {len(work)} experience and project bullets open with an action verb.")
    else:
        for i in [b for b in work if b not in started][:8]:
            mark(i, "warn", "Start with an action verb")
        _check(checks, "verbs", "Content", 6, "warn", "Action verbs",
               f"{len(started)} of {len(work)} experience and project bullets open with an action verb." if work
               else "No bullet points under your experience or projects to check.",
               "Open each bullet with what you did: Built, Designed, Automated, Reduced… (Développé, Conçu, Automatisé…).")

    pronouns = [(i, m.group(0)) for i, l in enumerate(lines) for m in _PRONOUN_RE.finditer(l)]
    if len(pronouns) >= 5:
        for i, _ in pronouns[:10]:
            mark(i, "warn", "Written in the first person")
        _check(checks, "pronouns", "Content", 4, "warn", "No “I” or “my”",
               f"{len(pronouns)} first-person words (“{pronouns[0][1]}”…). CVs are written without pronouns.",
               "Drop the pronoun and start with the verb: “Built a…” instead of “I built a…”.")
    else:
        _check(checks, "pronouns", "Content", 4, "ok", "No “I” or “my”", "Written without first-person pronouns.")

    if "skills" in found:
        end = min([i for i in found.values() if i > found["skills"]] + [len(lines)])
        items = [p for l in lines[found["skills"] + 1:end] for p in re.split(r"[,;|•·/]| - ", l) if len(p.strip()) > 1]
        if len(items) >= 6:
            _check(checks, "skills_list", "Content", 5, "ok", "Skills as keywords", f"{len(items)} skills listed — these are the words systems search for.")
        else:
            _check(checks, "skills_list", "Content", 5, "warn", "Skills as keywords", f"Only {len(items)} skill(s) listed.",
                   "List 8–15 concrete skills (tools, languages, methods) separated by commas.")
    else:
        _check(checks, "skills_list", "Content", 5, "fail", "Skills as keywords", "No skills section to read keywords from.",
               "Add a “Skills” section listing your tools, languages and methods, separated by commas.")

    long_lines = [i for i, l in enumerate(lines) if len(l) > 330 and not _bullet(l)]
    if long_lines:
        for i in long_lines[:6]:
            mark(i, "warn", "Long paragraph — split it into bullet points")
        _check(checks, "paragraphs", "Content", 5, "warn", "Short, scannable lines",
               f"{len(long_lines)} long paragraph(s), e.g. “{lines[long_lines[0]][:70]}…”.",
               "Break paragraphs into bullet points of one or two lines each.")
    else:
        _check(checks, "paragraphs", "Content", 5, "ok", "Short, scannable lines", "No long blocks of text.")

    # ---- Score -------------------------------------------------------------
    total = sum(c["points"] for c in checks)
    earned = sum(c["earned"] for c in checks)
    score = round(100 * earned / total) if total else 0
    blocking = any(c["id"] == "text" and c["status"] == "fail" for c in checks) or bool(meta.get("unsupported"))
    if blocking:
        score = min(score, 30)
    if score >= 85:
        label, tone = "Ready for ATS", "good"
    elif score >= 70:
        label, tone = "Good — a few fixes", "ok"
    elif score >= 50:
        label, tone = "Needs work", "warn"
    else:
        label, tone = "At risk of being misread", "bad"

    categories = []
    for name in ("Readable by machines", "Structure", "Content"):
        mine = [c for c in checks if c["category"] == name]
        categories.append({"name": name, "earned": sum(c["earned"] for c in mine),
                           "total": sum(c["points"] for c in mine)})
    order = {"fail": 0, "warn": 1, "ok": 2}
    return {
        "score": score, "label": label, "tone": tone, "blocking": blocking,
        "categories": categories,
        "checks": sorted(checks, key=lambda c: (order[c["status"]], -c["points"])),
        "to_fix": sum(1 for c in checks if c["status"] != "ok"),
        "missing_sections": [SECTION_NAMES[s] for s in missing],
        "lines": [{"text": l[:400], "mark": marks.get(i), "heading": _heading(l) is not None}
                  for i, l in enumerate(lines[:MAX_LINES_SHOWN])],
        "stats": {"pages": pages, "words": words, "images": meta.get("images", 0),
                  "kind": kind, "filename": meta.get("filename", "")},
    }


def check(filename: str, content: bytes) -> dict:
    lines, meta = inspect_file(filename, content)
    return evaluate(lines, meta)
