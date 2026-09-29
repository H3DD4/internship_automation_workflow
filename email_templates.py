"""The email styles a user can pick, each written in English and in French.

A template owns the connective wording — how the email opens, how the match
is introduced, how it closes. The user's profile owns every sentence about
them (who they are, what they built, what they're asking for), taken from
their CV and approved by them. build_spec() renders the two together into
the same spec shape as the original hand-written specializations.json, so
the composer, the research agent and the draft guard treat a template user
exactly like the account that was tuned by hand.

Two kinds of placeholders:
  <<identity>>   filled from the profile when the spec is built;
  {company}      filled per company by the composer ({hook}, {area_1},
                 {topic}, {applicant_name}, {target_role}, {company_de}).

The French wording is deliberately free of grammatical gender in the
template's own words ("Ce serait un plaisir", never "je serais ravi(e)"), so
it reads naturally for anyone; the profile's own French sentences carry
whatever form the user chose.
"""

from __future__ import annotations

import copy
import re

TEMPLATES = {
    # ------------------------------------------------------------------
    "specialist": {
        "name": {"en": "Specialist match", "fr": "Correspondance experte"},
        "description": {
            "en": "Opens on what the company does (quoted from its own site), matches it to your "
                  "strongest experience, then your other highlights. The proven default.",
            "fr": "S'ouvre sur l'activité de l'entreprise (citée depuis son site), la relie à votre "
                  "expérience la plus solide, puis vos autres atouts. Le modèle éprouvé par défaut.",
        },
        "en": {
            "subject": "{target_role} from <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "I'm <<identity>>. What interests me most about {company} is your work on {hook}.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'm really interested in becoming part of the team at {company}.",
                "I'm <<identity>>, and I'd genuinely like to join the team at {company}.",
                "I'm <<identity>>, and joining the {company} team is something I'm really interested in.",
            ],
            "match_lead_one": "Your focus on {area_1} is where my own experience is strongest.",
            "match_lead_two": "Your focus on {area_1} and {area_2} maps directly onto my own experience.",
            "closing_variants": [
                "My CV is attached with more detail. I'd be glad to discuss how I could contribute to {company}, and I look forward to hearing from you.",
                "My CV is attached with further detail. I'd be genuinely glad to bring this background to {company} and grow with the team — I look forward to hearing from you.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} à partir de <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>. Ce qui m'intéresse le plus chez {company}, c'est votre travail sur {hook}.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et rejoindre l'équipe {company_de} m'intéresse vraiment.",
                "Je suis <<identity>>, et j'aimerais sincèrement intégrer l'équipe {company_de}.",
                "Je suis <<identity>>, et faire partie de l'équipe {company_de} est une perspective qui me motive réellement.",
            ],
            "match_lead_one": "Votre expertise en {area_1} correspond précisément au domaine où mon expérience est la plus solide.",
            "match_lead_two": "Votre expertise en {area_1} et en {area_2} rejoint directement mon propre parcours.",
            "closing_variants": [
                "Vous trouverez mon CV en pièce jointe pour plus de détails. Ce serait un plaisir d'échanger sur la manière dont je pourrais contribuer à {company} ; dans l'attente de votre retour, je vous remercie de votre attention.",
                "Mon CV, joint à ce message, détaille davantage mon parcours. J'aimerais sincèrement mettre ce bagage au service de {company} et progresser avec l'équipe — au plaisir d'échanger avec vous.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "concise": {
        "name": {"en": "Short & direct", "fr": "Court et direct"},
        "description": {
            "en": "Four short paragraphs for busy recruiters: why them, your best match, one "
                  "highlight, the ask.",
            "fr": "Quatre paragraphes courts pour des recruteurs pressés : pourquoi eux, votre "
                  "meilleure correspondance, un atout, la demande.",
        },
        "layout": ["intro", "match", "strengths", "ask", "closing"],
        "strengths_budget": [1, 2],
        "include_motivation": False,
        "min_words": 70,
        "en": {
            "subject": "{target_role} ({topic}) — {applicant_name}",
            "intro_with_hook": "I'm <<identity>>, and your work on {hook} is exactly why I'm writing to {company}.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to join the team at {company}.",
                "I'm <<identity>>, and I'm writing because I'd like to join {company}.",
            ],
            "match_lead_one": "This lines up with my experience in {area_1}.",
            "match_lead_two": "This lines up with my experience in {area_1} and {area_2}.",
            "closing_variants": [
                "My CV is attached — I'd welcome a short call whenever it suits you.",
                "My CV is attached, and I'd be happy to talk whenever convenient.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} ({topic}) — {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>, et votre travail sur {hook} est précisément la raison de mon message à {company}.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais rejoindre l'équipe {company_de}.",
                "Je suis <<identity>>, et je vous écris car j'aimerais rejoindre {company}.",
            ],
            "match_lead_one": "Cela rejoint directement mon expérience en {area_1}.",
            "match_lead_two": "Cela rejoint directement mon expérience en {area_1} et en {area_2}.",
            "closing_variants": [
                "Mon CV est en pièce jointe ; un court échange serait un plaisir, quand cela vous convient.",
                "Vous trouverez mon CV en pièce jointe — au plaisir d'en discuter avec vous.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "project_led": {
        "name": {"en": "Project first", "fr": "Projet en premier"},
        "description": {
            "en": "Leads with your flagship project in the first line, then connects it to the "
                  "company. Strong when you have one standout achievement.",
            "fr": "Commence par votre projet phare dès la première ligne, puis le relie à "
                  "l'entreprise. Idéal quand une réalisation se démarque.",
        },
        "layout": ["flagship", "intro", "match", "strengths", "ask", "closing"],
        "strengths_budget": [1, 2],
        "en": {
            "subject": "{target_role} from <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "I'm <<identity>>, and what draws me to {company} is your work on {hook}.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to bring that same drive to the team at {company}.",
                "I'm <<identity>>, and I'd genuinely like to put that experience to work at {company}.",
            ],
            "match_lead_one": "Your focus on {area_1} is where I can contribute most directly.",
            "match_lead_two": "Your focus on {area_1} and {area_2} is where I can contribute most directly.",
            "closing_variants": [
                "My CV is attached with the details. I'd be glad to talk about how I could contribute to {company}.",
                "My CV is attached — I look forward to hearing from you.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} à partir de <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>, et ce qui m'attire chez {company}, c'est votre travail sur {hook}.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais mettre cette même énergie au service de l'équipe {company_de}.",
                "Je suis <<identity>>, et j'aimerais sincèrement mettre cette expérience au service de {company}.",
            ],
            "match_lead_one": "Votre expertise en {area_1} est le domaine où je peux contribuer le plus directement.",
            "match_lead_two": "Votre expertise en {area_1} et en {area_2} est le domaine où je peux contribuer le plus directement.",
            "closing_variants": [
                "Mon CV, en pièce jointe, donne tous les détails. Ce serait un plaisir d'échanger sur ma possible contribution chez {company}.",
                "Mon CV est en pièce jointe — au plaisir d'échanger avec vous.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "formal": {
        "name": {"en": "Formal", "fr": "Formel"},
        "description": {
            "en": "A classic, formal application — for large companies, public institutions and "
                  "traditional sectors.",
            "fr": "Une candidature classique et formelle — pour les grands groupes, les "
                  "institutions publiques et les secteurs traditionnels.",
        },
        "en": {
            "subject": "Application — {target_role} from <<start_date>> | {applicant_name}",
            "intro_with_hook": "I am <<identity>>, and I would like to apply for an internship at {company}, whose work on {hook} closely matches my interests.",
            "intro_standard_variants": [
                "I am <<identity>>, and I would like to apply for an internship at {company}.",
                "I am <<identity>>, and I am applying for an internship within {company}.",
            ],
            "match_lead_one": "Your activity in {area_1} corresponds closely to my own experience.",
            "match_lead_two": "Your activities in {area_1} and {area_2} correspond closely to my own experience.",
            "closing_variants": [
                "Please find my CV attached. I would welcome the opportunity to discuss how I could contribute to {company}. Thank you for your time and consideration.",
                "My CV is attached for your consideration. I remain at your disposal for an interview, and thank you for your attention.",
            ],
            "sign_off": "Kind regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "Candidature — {target_role} à partir de <<start_date>> | {applicant_name}",
            "intro_with_hook": "Actuellement <<identity>>, je me permets de vous adresser ma candidature pour un stage au sein de {company}, dont le travail sur {hook} rejoint pleinement mes centres d'intérêt.",
            "intro_standard_variants": [
                "Actuellement <<identity>>, je me permets de vous adresser ma candidature pour un stage au sein de {company}.",
                "Actuellement <<identity>>, je vous adresse ma candidature pour un stage au sein de {company}.",
            ],
            "match_lead_one": "Votre activité en {area_1} correspond étroitement à mon expérience.",
            "match_lead_two": "Vos activités en {area_1} et en {area_2} correspondent étroitement à mon expérience.",
            "closing_variants": [
                "Vous trouverez ci-joint mon CV. Je me tiens à votre disposition pour un entretien et vous prie d'agréer l'expression de mes salutations distinguées.",
                "Mon CV est joint à ce message. Restant à votre disposition pour un entretien, je vous prie d'agréer mes sincères salutations.",
            ],
            "sign_off": "{applicant_name}",
        },
    },
    # ------------------------------------------------------------------
    "research_lab": {
        "name": {"en": "Research & R&D", "fr": "Recherche & R&D"},
        "description": {
            "en": "For laboratories, R&D teams and research internships: frames your work as "
                  "research and asks to contribute to theirs.",
            "fr": "Pour les laboratoires, équipes R&D et stages de recherche : présente votre "
                  "travail comme de la recherche et propose de contribuer au leur.",
        },
        "en": {
            "subject": "Research {target_role} from <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "I'm <<identity>>. I'm writing because the research at {company} on {hook} is closely related to what I want to work on.",
            "intro_standard_variants": [
                "I'm <<identity>>, and I'd like to contribute to the research carried out at {company}.",
                "I'm <<identity>>, and I'm very interested in joining the research team at {company}.",
            ],
            "match_lead_one": "Your work in {area_1} is close to what I have done so far.",
            "match_lead_two": "Your work in {area_1} and {area_2} is close to what I have done so far.",
            "closing_variants": [
                "My CV is attached. I would be glad to discuss a possible research internship with your team.",
                "My CV is attached with more detail — I would be glad to discuss how I could contribute to your research.",
            ],
            "sign_off": "Best regards,\n{applicant_name}",
        },
        "fr": {
            "subject": "{target_role} en recherche à partir de <<start_date>> — {topic} | {applicant_name}",
            "intro_with_hook": "Je suis <<identity>>. Je vous écris car les recherches menées chez {company} sur {hook} sont très proches de ce sur quoi je souhaite travailler.",
            "intro_standard_variants": [
                "Je suis <<identity>>, et j'aimerais contribuer aux recherches menées chez {company}.",
                "Je suis <<identity>>, et rejoindre l'équipe de recherche {company_de} m'intéresse vivement.",
            ],
            "match_lead_one": "Vos travaux en {area_1} sont proches de ce que j'ai réalisé jusqu'ici.",
            "match_lead_two": "Vos travaux en {area_1} et en {area_2} sont proches de ce que j'ai réalisé jusqu'ici.",
            "closing_variants": [
                "Vous trouverez mon CV en pièce jointe. Ce serait un plaisir d'échanger sur un éventuel stage de recherche au sein de votre équipe.",
                "Mon CV, joint à ce message, détaille mon parcours — ce serait un plaisir d'échanger sur ma possible contribution à vos travaux.",
            ],
            "sign_off": "Cordialement,\n{applicant_name}",
        },
    },
}

DEFAULT_TEMPLATE = "specialist"
_STRUCTURE_KEYS = ("layout", "strengths_budget", "include_motivation", "min_words", "max_words")


def template_choices(lang: str = "en") -> list:
    return [{"id": tid, "name": t["name"][lang], "description": t["description"][lang]}
            for tid, t in TEMPLATES.items()]


def _escape(text: str) -> str:
    """Profile text goes into strings that are later str.format()-ed per
    company; a literal brace in someone's CV must stay a brace (and must not
    become a format field)."""
    return (text or "").replace("{", "{{").replace("}", "}}")


_NUMBER_RE = re.compile(r"\b\d[\d,.]*\b")


def _numbers(text: str) -> set:
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUMBER_RE.finditer(text or "")}


def _text(value, lang: str) -> str:
    if isinstance(value, dict):
        return (value.get(lang) or "").strip()
    return (value or "").strip()


def build_spec(facts: dict, template_id: str, lang: str) -> dict:
    """Profile facts + a template -> a composer spec for one language."""
    template = TEMPLATES.get(template_id) or TEMPLATES[DEFAULT_TEMPLATE]
    wording = copy.deepcopy(template[lang])
    fills = {
        "identity": _escape(_text(facts.get("identity"), lang)),
        "start_date": _escape(_text(facts.get("start_date"), lang)),
    }

    def render(value):
        if isinstance(value, list):
            return [render(v) for v in value]
        for key, filled in fills.items():
            value = value.replace(f"<<{key}>>", filled)
        return value

    email = {key: render(value) for key, value in wording.items()}
    for key in _STRUCTURE_KEYS:
        if key in template:
            email[key] = copy.deepcopy(template[key])
    # Hand-tuned wording guards at 120 words; a template has to fit shorter
    # CVs too, so its floor is lower (the guard still stops a stub email).
    email.setdefault("min_words", 90)
    email["default_topic"] = _text(facts.get("default_topic"), lang)
    email["motivation"] = _text(facts.get("motivation"), lang)
    email["internship_ask"] = _escape(_text(facts.get("internship_ask"), lang))

    areas = []
    for area in facts.get("areas") or []:
        evidence = _text(area.get("evidence"), lang)
        label = _text(area.get("label"), lang)
        if not (area.get("id") and label and evidence):
            continue
        built = {
            "id": area["id"],
            "label": label,
            "topic": _text(area.get("topic"), lang) or label,
            "match_description": (area.get("match_description") or label).strip(),
            "keywords": [k.lower() for k in area.get("keywords") or [] if k.strip()],
            "keywords_fr": [k.lower() for k in area.get("keywords_fr") or [] if k.strip()],
            "evidence": evidence,
            "sources": list(area.get("sources") or [area["id"]]),
        }
        if area.get("requires_any"):
            built["requires_any"] = list(area["requires_any"])
        if area.get("requires_any_fr"):
            built["requires_any_fr"] = list(area["requires_any_fr"])
        alt = _text(area.get("alt_evidence"), lang)
        if alt:
            built["alt_evidence"] = alt
            built["alt_sources"] = list(area.get("alt_sources") or [f"{area['id']}_alt"])
        areas.append(built)

    strengths = []
    for strength in facts.get("strengths") or []:
        text = _text(strength.get("text"), lang)
        if text:
            strengths.append({"id": strength.get("id") or f"s{len(strengths) + 1}",
                              "sources": list(strength.get("sources") or [strength.get("id") or text[:20]]),
                              "text": text})

    # Numbers the user approved in their own profile are the only ones an
    # email may state about them (the draft guard enforces it).
    allowed = set()
    for value in [email["motivation"], email["internship_ask"], fills["identity"], fills["start_date"],
                  *(a["evidence"] for a in areas), *(a.get("alt_evidence", "") for a in areas),
                  *(s["text"] for s in strengths)]:
        allowed |= _numbers(value)

    spec = {
        "verified_facts": {"allowed_numbers": sorted(allowed)},
        "target_role": _text(facts.get("target_role"), lang),
        "email": email,
        "areas": areas,
        "strengths": strengths,
    }
    return spec


def spec_problems(spec: dict) -> list:
    """What's missing for this spec to produce an email at all."""
    problems = []
    email = spec.get("email", {})
    if "<<" in email.get("intro_with_hook", "") or not email.get("intro_with_hook"):
        problems.append("the introduction")
    if not email.get("internship_ask"):
        problems.append("what you're asking for (the internship sentence)")
    if not email.get("default_topic"):
        problems.append("your headline topic")
    if not spec.get("areas"):
        problems.append("at least one area of experience")
    if not spec.get("strengths"):
        problems.append("at least one strength")
    return problems
