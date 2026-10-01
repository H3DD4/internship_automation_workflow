"""A user's profile: what the emails may say about them, in English and French.

How a new user gets emails as sharp as a hand-tuned account's:

  1. They upload their CV. Its text is extracted here (PDF / DOCX).
  2. The AI drafts the profile from that text only — who they are, 3 to 8
     areas of experience with concrete evidence, their strongest points —
     in both languages, in the same shape as the original hand-written
     specializations.json.
  3. Code checks every sentence against the CV (check_profile): a number or
     a proper name that isn't in the CV is flagged, and the profile can't be
     saved until the user fixes or explicitly confirms each flag. The AI
     suggests; the user's own CV — and the user — decide.
  4. The profile + the chosen template render the wording spec the composer
     uses (email_templates.build_spec).

The internship request itself isn't generated at all: it's assembled from
the user's own answers (type, start date, duration) by internship_ask().
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone

from defusedxml import ElementTree as SafeET
from sqlalchemy import select

import database
import email_templates
from database import profiles

LANGS = ("en", "fr")
MAX_CV_TEXT = 20000
MAX_AREAS = 8
MAX_STRENGTHS = 4
_ID_RE = re.compile(r"[^a-z0-9_]+")


class ProfileError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CV text
# ---------------------------------------------------------------------------

def extract_cv_text(filename: str, content: bytes) -> str:
    """Plain text of a PDF or DOCX CV (first pages only — a CV is short, and
    capping it bounds the work a hostile file can cause)."""
    name = (filename or "").lower()
    text = ""
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(content))
            pages = reader.pages[:6]
            text = "\n".join((page.extract_text() or "") for page in pages)
        except Exception as exc:  # malformed PDFs raise a zoo of errors
            raise ProfileError("Couldn't read text from that PDF. Is it a scanned image? "
                               "Export it again as a text PDF.") from exc
    elif name.endswith(".docx"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 5 * 1024 * 1024:
                    raise ProfileError("That Word file is unusually large.")
                root = SafeET.fromstring(archive.read(info))
        except (KeyError, zipfile.BadZipFile) as exc:
            raise ProfileError("That Word file couldn't be read.") from exc
        ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        paragraphs = []
        for paragraph in root.iter(f"{ns}p"):
            paragraphs.append("".join(node.text or "" for node in paragraph.iter(f"{ns}t")))
        text = "\n".join(p for p in paragraphs if p.strip())
    else:
        raise ProfileError("Automatic analysis works with PDF or DOCX CVs. "
                           "For a .doc file, save it as .docx or PDF first.")
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 200:
        raise ProfileError("Very little text could be read from the CV. If it's a scanned image, "
                           "export a text-based PDF from your editor.")
    return text[:MAX_CV_TEXT]


# ---------------------------------------------------------------------------
# The internship request, from the user's own answers
# ---------------------------------------------------------------------------

INTERNSHIP_KINDS = {
    "end_of_study": {"en": "an end-of-study internship", "fr": "un stage de fin d'études",
                     "role_en": "End-of-Study Internship", "role_fr": "Stage de fin d'études"},
    "internship": {"en": "an internship", "fr": "un stage",
                   "role_en": "Internship", "role_fr": "Stage"},
    "summer": {"en": "a summer internship", "fr": "un stage d'été",
               "role_en": "Summer Internship", "role_fr": "Stage d'été"},
    "research": {"en": "a research internship", "fr": "un stage de recherche",
                 "role_en": "Research Internship", "role_fr": "Stage de recherche"},
    "apprenticeship": {"en": "a work-study (apprenticeship) position", "fr": "une alternance",
                       "role_en": "Work-Study Position", "role_fr": "Alternance"},
    "first_job": {"en": "a first position", "fr": "un premier poste",
                  "role_en": "Junior Position", "role_fr": "Premier poste"},
}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
           "septembre", "octobre", "novembre", "décembre"],
}


def start_date_text(month: int, year: int, lang: str) -> str:
    if not (1 <= int(month) <= 12) or not (2000 <= int(year) <= 2100):
        raise ProfileError("Pick a valid start month and year.")
    return f"{MONTHS[lang][int(month) - 1]} {int(year)}"


def internship_ask(*, kind: str, month: int, year: int, duration_months: int | None,
                   degree_context: dict | None, open_to_hire: bool) -> dict:
    """{"en": ..., "fr": ...} — the request sentence, built from answers."""
    info = INTERNSHIP_KINDS.get(kind) or INTERNSHIP_KINDS["internship"]
    result = {}
    for lang in LANGS:
        start = start_date_text(month, year, lang)
        duration = ""
        if duration_months:
            duration = (f" for {int(duration_months)} months" if lang == "en"
                        else f" d'une durée de {int(duration_months)} mois")
        context = (degree_context or {}).get(lang, "").strip()
        if lang == "en":
            sentence = f"I'm looking for {info['en']}{duration} starting {start}"
            if context:
                sentence += f", {context}"
            sentence += (", and I'm open to continuing with the team afterwards." if open_to_hire else ".")
        else:
            sentence = f"Je recherche {info['fr']}{duration} à partir de {start}"
            if context:
                sentence += f", {context}"
            sentence += (", et poursuivre avec l'équipe par la suite serait une vraie opportunité pour moi."
                         if open_to_hire else ".")
        result[lang] = sentence
    return result


# ---------------------------------------------------------------------------
# AI draft of the profile
# ---------------------------------------------------------------------------

PROFILE_SYSTEM_PROMPT = """You turn a CV into the building blocks of job-application emails.
You use ONLY facts written in the CV. You never invent, estimate, round up or embellish.
If the CV doesn't say it, leave it out.

Write every text in English ("en") AND in natural, professional French ("fr").
In French, describe the person without guessing their gender: prefer neutral
constructions ("en dernière année de ...", "titulaire de ...") over
"étudiant/étudiante" unless the CV itself makes the form clear.

Return ONLY this JSON object:
{
  "identity": {"en": "", "fr": ""},
  "default_topic": {"en": "", "fr": ""},
  "motivation": {"en": "", "fr": ""},
  "areas": [
    {"id": "", "label": {"en": "", "fr": ""}, "topic": {"en": "", "fr": ""},
     "match_description": "", "keywords": [], "keywords_fr": [],
     "evidence": {"en": "", "fr": ""}, "sources": [],
     "alt_evidence": {"en": "", "fr": ""}, "alt_sources": []}
  ],
  "strengths": [ {"id": "", "text": {"en": "", "fr": ""}, "sources": []} ]
}

identity: the noun phrase that follows "I'm" / "Je suis", e.g.
  en "a final-year Computer Engineering student at ENSIT in Tunis, specialised in cybersecurity and AI engineering"
  fr "en dernière année du cycle ingénieur en informatique à l'ENSIT (Tunis), avec une spécialisation en cybersécurité et en IA"
  (English starts with "a"/"an"; French has no leading article.)
default_topic: 2-5 words naming their field, for the email subject.
motivation: one sentence, first person, on what drives them — only if the CV supports it, else "".

areas: 3 to 8 fields of work this person can credibly apply to, drawn from their
experience, projects and studies — whatever their discipline (engineering,
finance, marketing, law, biology...). For each:
  id: short snake_case.
  label: lower-case noun phrase that reads after "Your focus on" / "Votre expertise en"
         (fr without article: "analyse financière", not "l'analyse financière").
  topic: Title Case, 2-5 words, for the subject line.
  match_description: English, one line describing the kind of COMPANY this fits.
  keywords: 6-15 lower-case English words/phrases such a company's website would use.
  keywords_fr: the same idea in French, as French websites write it.
  evidence: ONE first-person sentence with a concrete result from the CV
            (what they built/did, where, and the measurable outcome if the CV gives one).
  sources: short ids of the project/job/course the evidence comes from.
  alt_evidence / alt_sources: a second, DIFFERENT source for the same area, or empty.
  Two areas should not rely on the same single source when the CV offers alternatives.

strengths: 2 to 3 first-person sentences on their strongest points (flagship project,
experience, awards, leadership), each with "sources" ids. The first one is their
single most impressive achievement.

Never name tools or libraries in a sentence (the CV lists them). No flattery, no
"passionate", no "hard-working". Numbers only exactly as written in the CV."""


def _clean_id(value: str, fallback: str) -> str:
    value = _ID_RE.sub("_", (value or "").lower()).strip("_")[:40]
    return value or fallback


def _pair(value) -> dict:
    if isinstance(value, dict):
        return {lang: str(value.get(lang) or "").strip() for lang in LANGS}
    text = str(value or "").strip()
    return {"en": text, "fr": ""}


def _string_list(value, limit: int = 20) -> list:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:80] for v in value if str(v).strip()][:limit]


def normalize_facts(raw: dict) -> dict:
    """Coerce a profile (from the AI or from the editor form) into the exact
    shape the rest of the app expects. Drops anything unknown."""
    facts = {
        "full_name": str(raw.get("full_name") or "").strip()[:120],
        "identity": _pair(raw.get("identity")),
        "default_topic": _pair(raw.get("default_topic")),
        "motivation": _pair(raw.get("motivation")),
        "internship_ask": _pair(raw.get("internship_ask")),
        "start_date": _pair(raw.get("start_date")),
        "target_role": _pair(raw.get("target_role")),
        "internship": raw.get("internship") if isinstance(raw.get("internship"), dict) else {},
        "confirmed": sorted({str(x) for x in raw.get("confirmed") or [] if str(x)})[:200],
        "areas": [],
        "strengths": [],
    }
    seen = set()
    for index, area in enumerate((raw.get("areas") or [])[:MAX_AREAS]):
        if not isinstance(area, dict):
            continue
        area_id = _clean_id(area.get("id"), f"area_{index + 1}")
        while area_id in seen:
            area_id += "_2"
        seen.add(area_id)
        facts["areas"].append({
            "id": area_id,
            "label": _pair(area.get("label")),
            "topic": _pair(area.get("topic")),
            "match_description": str(area.get("match_description") or "").strip()[:300],
            "keywords": _string_list(area.get("keywords")),
            "keywords_fr": _string_list(area.get("keywords_fr")),
            "requires_any": _string_list(area.get("requires_any"), 10),
            "requires_any_fr": _string_list(area.get("requires_any_fr"), 10),
            "evidence": _pair(area.get("evidence")),
            "sources": [_clean_id(s, area_id) for s in _string_list(area.get("sources"), 5)] or [area_id],
            "alt_evidence": _pair(area.get("alt_evidence")),
            "alt_sources": [_clean_id(s, area_id + "_alt") for s in _string_list(area.get("alt_sources"), 5)],
        })
    for index, strength in enumerate((raw.get("strengths") or [])[:MAX_STRENGTHS]):
        if not isinstance(strength, dict):
            continue
        sid = _clean_id(strength.get("id"), f"strength_{index + 1}")
        facts["strengths"].append({
            "id": sid,
            "text": _pair(strength.get("text")),
            "sources": [_clean_id(s, sid) for s in _string_list(strength.get("sources"), 5)] or [sid],
        })
    return facts


def draft_profile_with_ai(router, cv_text: str) -> dict:
    """Ask the user's own AI pool for a first draft. Raises ProfileError."""
    from agents.research_agent import _parse_json_object
    try:
        result = router.complete(task="research", system=PROFILE_SYSTEM_PROMPT,
                                 messages=[{"role": "user", "content": "CV:\n\n" + cv_text}],
                                 max_tokens=6000, temperature=0.2, json_mode=True,
                                 reasoning_effort="medium", max_wait=120)
        answer = _parse_json_object(result.text)
    except Exception as exc:
        raise ProfileError(f"The AI couldn't draft your profile ({str(exc)[:160]}). "
                           "Check your AI key in Settings and try again.") from exc
    facts = normalize_facts(answer)
    if not facts["areas"] or not facts["strengths"]:
        raise ProfileError("The AI's draft came back incomplete. Try again, or try another model.")
    return facts


# ---------------------------------------------------------------------------
# Grounding: every claim checked against the CV
# ---------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_CAP_WORD_RE = re.compile(r"(?<![\w'’])([A-Z][\w&.\-]*[A-Za-z0-9])")
_ALWAYS_OK_WORDS = {
    "I", "I'm", "I've", "I'd", "AI", "IA", "CV", "CTF", "CTFs", "LLM", "LLMs", "RAG", "SOC",
    "Bac", "English", "French", "Anglais", "Français", "Je", "Mon", "Ma", "Mes", "Chez", "My",
    "At", "In", "As", "Le", "La", "Les", "Un", "Une", "En", "Au", "Dans", "Pour", "Lors",
    "Web", "PhD", "MSc", "BSc", "Master", "Licence", "Bachelor", "Engineering", "Computer",
    "Science", "Associate", "Solutions", "Architect",
} | set(sum((m for m in (MONTHS["en"], [x.capitalize() for x in MONTHS["fr"]])), []))


def _numbers_in(text: str) -> set:
    return {re.sub(r"[.,]", "", n) for n in _NUMBER_RE.findall(text or "")}


def _field_texts(facts: dict) -> list:
    """(field_key, label, text) for every CV-derived sentence in the profile."""
    fields = []
    for lang in LANGS:
        for key in ("identity", "motivation", "default_topic"):
            fields.append((f"{key}.{lang}", f"{key} ({lang.upper()})", facts[key].get(lang, "")))
        for area in facts["areas"]:
            for key in ("evidence", "alt_evidence", "label"):
                fields.append((f"area.{area['id']}.{key}.{lang}",
                               f"{area['label'].get('en') or area['id']} — {key.replace('_', ' ')} ({lang.upper()})",
                               area[key].get(lang, "")))
        for strength in facts["strengths"]:
            fields.append((f"strength.{strength['id']}.{lang}", f"strength ({lang.upper()})",
                           strength["text"].get(lang, "")))
    return [f for f in fields if f[2]]


def check_profile(facts: dict, cv_text: str) -> list:
    """Claims the CV doesn't back up: [{"key", "label", "text", "reason"}].
    Numbers must appear in the CV; so must names (capitalised words that
    aren't sentence starts or common words). A flag is cleared by editing the
    text or by confirming it (facts["confirmed"] holds confirmed keys)."""
    cv_lower = (cv_text or "").lower()
    cv_numbers = _numbers_in(cv_text)
    confirmed = set(facts.get("confirmed") or [])
    flags = []
    for key, label, text in _field_texts(facts):
        if key in confirmed:
            continue
        reasons = []
        missing_numbers = sorted(n for n in _numbers_in(text) if n not in cv_numbers)
        if missing_numbers:
            reasons.append("number(s) not in your CV: " + ", ".join(missing_numbers))
        names = []
        for sentence in re.split(r"(?<=[.!?:;—])\s+", text):
            for index, match in enumerate(_CAP_WORD_RE.finditer(sentence)):
                word = match.group(1).rstrip(".")
                if match.start() == 0 or word in _ALWAYS_OK_WORDS or len(word) < 2:
                    continue
                if word.lower() not in cv_lower and word not in names:
                    names.append(word)
        if names:
            reasons.append("name(s) not found in your CV: " + ", ".join(names[:6]))
        if reasons:
            flags.append({"key": key, "label": label, "text": text, "reason": "; ".join(reasons)})
    return flags


def completeness_problems(facts: dict) -> list:
    problems = []
    if not facts.get("full_name"):
        problems.append("your name")
    for lang, name in (("en", "English"), ("fr", "French")):
        if not facts["identity"].get(lang):
            problems.append(f"how you introduce yourself ({name})")
        if not facts["internship_ask"].get(lang):
            problems.append(f"your internship request ({name})")
        if not facts["target_role"].get(lang):
            problems.append(f"the role you're applying for ({name})")
    if not any(a["evidence"].get("en") for a in facts["areas"]):
        problems.append("at least one area of experience with evidence")
    if not any(s["text"].get("en") for s in facts["strengths"]):
        problems.append("at least one strength")
    return problems


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def load(user_id: int) -> dict | None:
    with database.read() as conn:
        row = conn.execute(select(profiles).where(profiles.c.user_id == int(user_id))).first()
    if not row:
        return None
    data = dict(row._mapping)
    for key in ("facts", "spec_en", "spec_fr"):
        data[key] = json.loads(data[key]) if data.get(key) else None
    return data


def save(user_id: int, **fields) -> None:
    allowed = {"mode", "template_id", "language_mode", "facts", "spec_en", "spec_fr", "cv_text"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown profile field(s): {unknown}")
    values = {}
    for key, value in fields.items():
        values[key] = json.dumps(value, ensure_ascii=False) if key in ("facts", "spec_en", "spec_fr") and value is not None else value
    current = load(user_id)
    row = {"user_id": int(user_id), "updated_at": _now(),
           "mode": (current or {}).get("mode") or "template",
           "template_id": (current or {}).get("template_id") or email_templates.DEFAULT_TEMPLATE,
           "language_mode": (current or {}).get("language_mode") or "auto"}
    if current:
        for key in ("facts", "spec_en", "spec_fr"):
            row[key] = json.dumps(current[key], ensure_ascii=False) if current.get(key) is not None else None
        row["cv_text"] = current.get("cv_text")
    row.update(values)
    with database.tx() as conn:
        database.upsert(conn, profiles, row, ["user_id"])


def apply_template(user_id: int, facts: dict, template_id: str) -> dict:
    """Render and store both language specs from facts + template."""
    if template_id not in email_templates.TEMPLATES:
        raise ProfileError("Unknown template.")
    specs = {lang: email_templates.build_spec(facts, template_id, lang) for lang in LANGS}
    save(user_id, mode="template", template_id=template_id, facts=facts,
         spec_en=specs["en"], spec_fr=specs["fr"])
    return specs


def specs_for(user_id: int) -> tuple[dict, dict | None, dict | None]:
    """(profile row or {}, spec_en, spec_fr) — the specs the composer uses."""
    profile = load(user_id) or {}
    return profile, profile.get("spec_en"), profile.get("spec_fr")


def is_ready(user_id: int) -> bool:
    _, spec_en, _ = specs_for(user_id)
    return bool(spec_en and not email_templates.spec_problems(spec_en))
