"""
Agent 2: Writer Agent
Combines:
  - the applicant's fixed core_identity pitch, every fact of which is
    verifiable in the attached CV
  - the company research JSON (from research_agent): its working_axes feed
    the one company-specific paragraph, and 0-2 matched extra_mentions add a
    ready-made sentence each
into a final, ready-to-send email.

MOSTLY ASSEMBLY, ONE PARAGRAPH OF COMPOSITION. The applicant's story is
fixed text that only gets lightly formatted. Exactly one paragraph — "what
draws me to this company specifically" — is actually written per company,
and it is fenced hard: it may only use facts from COMPANY_RESEARCH, and
draft_guard.py rejects the result if a number or claim appears that isn't
traceable to the CV facts or the research. That keeps the freedom where it
earns its keep (a genuinely personal paragraph, which is what makes these
emails land) and removes it everywhere else.
"""

import json

from retry import with_retry
from ai_client import extract_json_object, build_model_fallback_list
from agents.draft_guard import GuardRejection, check_draft


WRITER_SYSTEM_PROMPT = """You write one job application email. You are mostly
an ASSEMBLER: almost all the content is given to you as approved text you
reproduce faithfully. Exactly ONE paragraph is yours to write, and it is
tightly constrained. When in doubt, change LESS, not more.

You will receive:
1. CORE_IDENTITY_TEXT — the applicant's story as a numbered paragraph plan,
   with the company name already filled in. Every fact in it is verified.
   This IS the email, almost word for word.
2. COMPANY_RESEARCH — what was actually found on this company's website:
   working_axes (concrete things they build or run), evidence, industry,
   mission. May be empty or "unknown".
3. COMPANY_PARAGRAPH_RULES — the rules for the one paragraph you write.
4. GREETING_LINE — copy EXACTLY as the first line. Never guess a name.
5. APPLICANT_NAME — for the sign-off only.
6. COMPANY_NAME, TARGET_ROLE — for the subject line.
7. EXTRA_SENTENCES — 0-2 ready-made sentences to insert (may be empty).

WHAT YOU DO:
1. Follow CORE_IDENTITY_TEXT's numbered plan in order, turning each point
   into a flowing paragraph. Keep every point, keep every number exactly as
   written, keep the same level of detail. Do not summarise or shorten.
2. Write paragraph 2 ("What draws me to {COMPANY_NAME} specifically is...")
   using COMPANY_RESEARCH and obeying COMPANY_PARAGRAPH_RULES to the letter.
   If COMPANY_RESEARCH has no working_axes and its industry is "unknown",
   OMIT this paragraph completely — a missing paragraph is always better
   than a invented one.
3. Insert each EXTRA_SENTENCE exactly once, close to word-for-word. You may
   adjust only a leading connector ("Also," / "In addition,"). Never add a
   sentence you were not given.
4. Copy GREETING_LINE as the first line; sign off with APPLICANT_NAME.

HARD RULES — BREAKING ANY OF THESE FAILS THE DRAFT:
- Never state a number that is not in CORE_IDENTITY_TEXT. Not the applicant's
  numbers changed, not a new number about the company. No years, no
  headcounts, no client counts, no funding, no founding dates.
- Never claim anything about the company that COMPANY_RESEARCH does not say.
  No "industry leader", "award-winning", "fast-growing", "renowned" — those
  are inventions unless the research states them.
- Never claim the applicant used the company's product, met anyone there,
  read their blog, attended their event, or saw a specific job posting.
- Never add achievements, technologies or experience to the applicant beyond
  CORE_IDENTITY_TEXT and EXTRA_SENTENCES.
- Never change GREETING_LINE. Never write "Dear Hiring Manager" yourself.
- No filler openings ("I am writing to express my keen interest"), no
  "I hope this email finds you well", no corporate padding.

LENGTH: 230-330 words in the body, excluding greeting and sign-off.

Respond with ONLY this JSON object — no markdown fences, nothing before or
after it:
{
  "subject": "short, specific subject line naming the company and the role",
  "body": "the full email body as plain text, with \\n\\n between paragraphs"
}
"""


def generate_email(client, model: str, core_identity: dict, applicant_name: str,
                    company_context: dict, company_name: str,
                    extra_mentions_config: list, target_role: str, greeting: str,
                    company_paragraph_rules: dict = None,
                    allow_fallback: bool = True) -> dict:
    """
    Produces {"subject": ..., "body": ...} for one company, verified by
    draft_guard before it is returned.

    `greeting` is the pre-resolved exact opening line (e.g. "Hello Charly,"
    or "Hello Rtone team,") — computed in utils.build_greeting, not left to
    the AI to guess. `company_paragraph_rules` is specializations.json's
    company_paragraph block, governing the one per-company paragraph.
    """
    matched_ids = company_context.get("matched_extra_mentions", [])

    # Each matched extra_mention contributes its pre-written `sentence`
    # as-is — the model is asked to insert it near-verbatim, not to write
    # its own sentence from a topic instruction. Less for a small/free
    # model to improvise, less that can drift from the approved wording.
    extra_sentences = [
        m["sentence"] for m in extra_mentions_config if m["id"] in matched_ids
    ][:2]  # hard cap of 2, matching the system prompt's rule

    # Only the parts of the research the company paragraph may draw on, so
    # the model isn't tempted by fields that aren't evidence.
    research_for_prompt = {
        key: company_context.get(key)
        for key in ("industry", "mission_or_focus", "working_axes", "evidence",
                     "talking_points", "notable_products_or_news", "tone_of_voice")
        if company_context.get(key)
    }

    payload = {
        "CORE_IDENTITY_TEXT": core_identity["angle_prompt"].replace(
            "{company_name}", company_name
        ),
        "COMPANY_RESEARCH": research_for_prompt or "No usable research — omit the company paragraph.",
        "COMPANY_PARAGRAPH_RULES": company_paragraph_rules or {},
        "APPLICANT_NAME": applicant_name,
        "GREETING_LINE": greeting,
        "COMPANY_NAME": company_name,
        "TARGET_ROLE": target_role,
        "EXTRA_SENTENCES": extra_sentences,
    }
    facts = core_identity.get("verified_facts", {})

    # Transport-level errors (network, 429, 5xx) are already retried inside
    # CompatibleAIClient.messages.create; the retry here covers a successful
    # HTTP call that came back empty, unparsable, or carrying an invented
    # claim — in the last case the guard's complaint is fed back so the model
    # fixes that specific problem instead of rerolling blindly.
    # allow_fallback=False keeps a single-model evaluation honest: with the
    # chain on, check_models.py would report another model's error against
    # the one being tested.
    candidates = build_model_fallback_list(model) if allow_fallback else [model]

    last_err = None
    for attempt_model in candidates:
        complaint = {"text": None}

        def _call_and_parse(model_name=attempt_model):
            messages = [{"role": "user", "content": json.dumps(payload, indent=2)}]
            if complaint["text"]:
                messages.append({
                    "role": "user",
                    "content": ("Your previous draft was rejected by a verification step. "
                                 f"Fix exactly this and return the corrected JSON:\n{complaint['text']}"),
                })
            resp = client.messages.create(
                model=model_name,
                max_tokens=4096,
                system=WRITER_SYSTEM_PROMPT,
                messages=messages,
                # Assembly with one constrained paragraph: low enough to keep
                # the fixed text faithful, not so low the one written
                # paragraph reads mechanically.
                temperature=0.3,
                reasoning_effort="low",
            )
            raw_text = resp.content[0].text.strip()
            if not raw_text:
                raise ValueError("Writer agent returned empty content")
            cleaned = extract_json_object(raw_text) or raw_text
            data = json.loads(cleaned)
            if "subject" not in data or "body" not in data:
                raise ValueError(f"Writer agent response missing subject/body: {data}")
            draft = {
                "subject": (data.get("subject") or "").strip(),
                "body": (data.get("body") or "").strip(),
            }
            if not draft["subject"] or not draft["body"]:
                raise ValueError(f"Writer agent returned empty subject/body: {data}")

            try:
                check_draft(draft, facts=facts, research=company_context,
                             company_name=company_name, greeting=greeting,
                             applicant_name=applicant_name,
                             extra_sentences=extra_sentences)
            except GuardRejection as rejection:
                # Log the first rejection per model only — a retry that keeps
                # failing the same way would otherwise print the same wall of
                # text three times per company.
                if complaint["text"] is None:
                    print(f"    [writer] {attempt_model} draft rejected for "
                          f"{company_name}: {str(rejection)[:180]}")
                complaint["text"] = str(rejection)
                raise
            return draft

        try:
            result = with_retry(
                _call_and_parse,
                attempts=3, base_delay=1.0,
                retry_on=(ValueError, json.JSONDecodeError, GuardRejection),
                what=f"Writer call for {company_name} ({attempt_model})",
                quiet=True,
            )
            if attempt_model != model:
                print(f"    [writer] used fallback model {attempt_model} for {company_name}")
            return result
        except Exception as e:
            last_err = e
            print(f"    [writer] model {attempt_model} failed for {company_name}: {e}")
            continue

    raise last_err or RuntimeError(f"All writer models failed for {company_name}")
