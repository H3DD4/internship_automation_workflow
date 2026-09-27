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


def _pick(variants: list, key: str) -> str:
    """Stable choice: Python's hash() is salted per process, sha256 isn't."""
    if not variants:
        return ""
    digest = hashlib.sha256(key.strip().lower().encode("utf-8")).digest()
    return variants[digest[0] % len(variants)]


def _select_evidence(area_ids: list, areas: list) -> tuple:
    """Pick one evidence sentence per area, never two drawing on the same
    project or internship (falling back to an area's alt_evidence, or
    dropping the area). Returns ([(area, sentence)], set_of_used_sources)."""
    by_id = {area["id"]: area for area in areas}
    used, chosen = set(), []
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


def compose_email(spec: dict, research: dict, company_name: str, greeting: str,
                  applicant_name: str, target_role: str) -> dict:
    """Return {"subject", "body"} for one company, checked by draft_guard.

    `research` is research_agent's verified output: `company_hook` (a phrase
    confirmed against their site, or "") and `areas` (up to two area ids
    confirmed by keywords on their site, or [])."""
    research = research or {}
    email = spec["email"]
    fields = {"company": company_name, "applicant_name": applicant_name,
              "target_role": target_role}

    hook = (research.get("company_hook") or "").strip()
    area_ids = list(research.get("areas") or [])[:2]

    # 1. Intro + interest
    if hook:
        intro = email["intro_with_hook"].format(hook=hook, **fields)
    else:
        intro = _pick(email["intro_standard_variants"], company_name).format(**fields)
    paragraphs = [intro]

    # 2. Match their work to the CV
    chosen, used_sources = _select_evidence(area_ids, spec["areas"])
    if len(chosen) == 2:
        lead = email["match_lead_two"].format(area_1=chosen[0][0]["label"],
                                              area_2=chosen[1][0]["label"])
    elif chosen:
        lead = email["match_lead_one"].format(area_1=chosen[0][0]["label"])
    if chosen:
        paragraphs.append(" ".join([lead] + [sentence for _, sentence in chosen]))

    # 3. Broader strengths, skipping anything already cited
    budget = 2 if chosen else 3
    strengths = [s["text"] for s in spec["strengths"]
                 if not set(s["sources"]) & used_sources][:budget]
    paragraphs.append(" ".join(strengths + [email["motivation"]]))

    # 4-5. The ask, then the close
    paragraphs.append(email["internship_ask"].format(**fields))
    paragraphs.append(_pick(email["closing_variants"], f"{company_name}#closing").format(**fields))

    body = "\n\n".join([greeting, *paragraphs, email["sign_off"].format(**fields)])
    topic = chosen[0][0]["topic"] if chosen else email["default_topic"]
    draft = {"subject": email["subject"].format(topic=topic, **fields), "body": body}

    try:
        check_draft(draft, facts=spec["verified_facts"], research=research,
                    company_name=company_name, greeting=greeting,
                    applicant_name=applicant_name)
    except GuardRejection:
        # Everything except the hook is CV text, so a hook is the only thing
        # that can trip the guard at runtime. Drop it and fall back to the
        # standard interest line rather than lose the company.
        if hook:
            return compose_email(spec, {**research, "company_hook": ""}, company_name,
                                 greeting, applicant_name, target_role)
        raise
    return draft
