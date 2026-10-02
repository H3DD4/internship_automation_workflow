"""Builds the application email from specializations.json. No AI involved.

Free-tier models are the weak link in this pipeline: asked to "write an
email", they embellish, and in a job application an embellished claim is
worse than no claim. So the model is only used upstream, in research_agent,
to answer two narrow questions whose answers code can verify against the
company's own website. This module turns those verified answers into the
email, using only sentences written from the CV.

Structure (the order a recruiter reads in):
  1. Who I am + what interests me about this company
       - with a verified hook: "What interests me most about X is your work on <phrase from their site>."
       - without one: a plain "I'm really interested in joining the team at X."
  2. Match: the one or two areas of the CV that fit what they do, each backed
     by a concrete result. Omitted when nothing on their site matched.
  3. Broader strengths: the CV's other strong points, never repeating a
     project already used in (2).
  4. The internship ask: end-of-study, Bac+5, from February 2027.
  5. Closing with the CV attached.

The same company always gets the same email (variants are chosen by a
stable hash of the name), while two different companies with no research
don't get byte-identical bodies, which spam filters penalise.
"""

import hashlib

from agents.draft_guard import GuardRejection, check_draft
from language import french_de


def _pick(variants: list, key: str) -> str:
    """Stable choice: Python's hash() is salted per process, sha256 isn't."""
    if not variants:
        return ""
    digest = hashlib.sha256(key.strip().lower().encode("utf-8")).digest()
    return variants[digest[0] % len(variants)]


def _select_evidence(area_ids: list, areas: list, used: set | None = None) -> tuple:
    """Pick one evidence sentence per area, never two drawing on the same
    project or internship (falling back to an area's alt_evidence, or
    dropping the area). `used` holds sources already cited earlier in the
    email. Returns ([(area, sentence)], set_of_used_sources)."""
    by_id = {area["id"]: area for area in areas}
    used, chosen = set(used or ()), []
    for area_id in area_ids:
        area = by_id.get(area_id)
        if not area:
            continue
        for text_key, source_key in (("evidence", "sources"), ("alt_evidence", "alt_sources")):
            text = area.get(text_key)
            sources = set(area.get(source_key, []))
            if text and not sources & used:
                chosen.append((area, text))
                used |= sources
                break
    return chosen, used


# The paragraph order of the original email. A template can reorder, drop or
# add sections ("flagship" opens with the strongest project); a spec without a
# layout — every hand-written specializations.json — gets exactly this.
DEFAULT_LAYOUT = ("intro", "match", "strengths", "ask", "closing")


def compose_email(spec: dict, research: dict, company_name: str, greeting: str,
                  applicant_name: str, target_role: str, lang: str = "en") -> dict:
    """Return {"subject", "body"} for one company, checked by draft_guard.

    `research` is research_agent's verified output: `company_hook` (a phrase
    confirmed against their site, or "") and `areas` (up to two area ids
    confirmed by keywords on their site, or []). `spec` is the wording in the
    language being written (`lang`)."""
    research = research or {}
    email = spec["email"]
    fields = {"company": company_name, "applicant_name": applicant_name,
              "target_role": target_role, "company_de": french_de(company_name)}

    hook = (research.get("company_hook") or "").strip()
    area_ids = list(research.get("areas") or [])[:2]
    layout = email.get("layout") or DEFAULT_LAYOUT
    strengths = list(spec.get("strengths") or [])

    used_sources: set = set()
    paragraphs, chosen = [], []
    flagship_id = None
    if "flagship" in layout and strengths:
        # Opening with the strongest project: it is cited here, so the match
        # paragraph below won't cite it again.
        flagship_id = strengths[0].get("id")
        used_sources |= set(strengths[0].get("sources", []))
    chosen, used_sources = _select_evidence(area_ids, spec["areas"], used_sources)

    for section in layout:
        if section == "flagship" and flagship_id is not None:
            lead = email.get("flagship_lead", "")
            text = strengths[0]["text"]
            paragraphs.append(f"{lead.format(**fields)} {text}".strip() if lead else text)
        elif section == "intro":
            if hook:
                intro = email["intro_with_hook"].format(hook=hook, **fields)
            else:
                intro = _pick(email["intro_standard_variants"], company_name).format(**fields)
            paragraphs.append(intro)
        elif section == "match" and chosen:
            if len(chosen) == 2:
                lead = email["match_lead_two"].format(area_1=chosen[0][0]["label"],
                                                      area_2=chosen[1][0]["label"], **fields)
            else:
                lead = email["match_lead_one"].format(area_1=chosen[0][0]["label"], **fields)
            paragraphs.append(" ".join([lead] + [sentence for _, sentence in chosen]))
        elif section == "strengths":
            with_match, without_match = email.get("strengths_budget", (2, 3))
            budget = with_match if chosen else without_match
            picked = [s["text"] for s in strengths
                      if s.get("id") != flagship_id and not set(s["sources"]) & used_sources][:budget]
            tail = [email["motivation"]] if email.get("include_motivation", True) and email.get("motivation") else []
            if picked or tail:
                paragraphs.append(" ".join(picked + tail))
        elif section == "ask":
            # A style may phrase the ask itself (a short call instead of a
            # job); otherwise it's the user's own internship sentence.
            paragraphs.append((email.get("ask_text") or email["internship_ask"]).format(**fields))
        elif section == "closing":
            paragraphs.append(_pick(email["closing_variants"], f"{company_name}#closing").format(**fields))

    body = "\n\n".join([greeting, *paragraphs, email["sign_off"].format(**fields)])
    topic = chosen[0][0]["topic"] if chosen else email["default_topic"]
    draft = {"subject": email["subject"].format(topic=topic, **fields), "body": body}

    try:
        check_draft(draft, facts=spec["verified_facts"], research=research,
                    company_name=company_name, greeting=greeting,
                    applicant_name=applicant_name,
                    min_words=email.get("min_words", 120), max_words=email.get("max_words", 400))
    except GuardRejection:
        # Everything except the hook is CV text, so a hook is the only thing
        # that can trip the guard at runtime. Drop it and fall back to the
        # standard interest line rather than lose the company.
        if hook:
            return compose_email(spec, {**research, "company_hook": ""}, company_name,
                                 greeting, applicant_name, target_role, lang)
        raise
    return draft
