"""
Agent 2: Writer Agent
Combines:
  - the user's fixed core_identity pitch (RedBox, cybersecurity, agentic AI)
  - the company research JSON (from research_agent), including 0+ matched
    extra_mentions (cloud / software_dev / data / etc.)
into a final, ready-to-send email.

Note: the core_identity prompt already fully covers the applicant's story,
so no CV-parsing/profile step is used here — the CV file is only attached
as-is when sending (see mailer.py).

DELIBERATELY LOW-FREEDOM BY DESIGN: this project is built to work with
cheap/free-tier models, which follow narrow, mechanical instructions far
more reliably than open-ended "be a great writer" ones. So the writer
agent's job is framed as assembly, not composition: the core_identity text
is treated as already-approved and is only lightly formatted, and each
extra_mention is a ready-made sentence to insert near-verbatim rather than
an open instruction to write something about a topic. This keeps model
output close to input, which is exactly what makes it both fast to verify
and safe to run unattended.
"""

import json

from retry import with_retry
from ai_client import extract_json_object, build_model_fallback_list


WRITER_SYSTEM_PROMPT = """You are a TEMPLATE ASSEMBLER, not a creative writer.
CORE_IDENTITY_TEXT below is already final and already approved — your job is
to output it as a clean email, not to improve it, not to reinterpret it, and
not to add your own ideas to it. When in doubt, change LESS, not more.

You will receive:
1. CORE_IDENTITY_TEXT — the exact facts, in the exact order, with the
   company name already filled in. This IS the email content, almost word
   for word.
2. APPLICANT_NAME — use only for the sign-off.
3. GREETING_LINE — copy this EXACTLY as the first line. Never write a
   different greeting, never guess a name yourself.
4. COMPANY_NAME, TARGET_ROLE — use ONLY to build the subject line. Do not
   pull in any other facts about the company that aren't already given to
   you — you were not given a company description on purpose.
5. EXTRA_SENTENCES — a list of 0, 1, or 2 ready-made sentences to insert
   (may be empty).

THE ONLY THINGS YOU ARE ALLOWED TO DO:
1. Turn the numbered points in CORE_IDENTITY_TEXT into flowing paragraphs:
   fix grammar, add paragraph breaks. Keep ALL points, in the SAME order,
   with the SAME level of detail. Do not summarize or shorten any of them.
2. If EXTRA_SENTENCES is not empty: insert EACH sentence given to you
   EXACTLY ONCE, copied close to word-for-word. You may only adjust a
   leading connector word ("Also," / "In addition,") so it flows with the
   sentence right before it — do NOT paraphrase, expand, shrink, or reword
   the sentence itself. Place it right after the professional-experience
   paragraph or the AI-connection paragraph, whichever reads more
   naturally. Never insert more than the sentences you were given.
3. Copy GREETING_LINE exactly as the first line. Sign off with
   APPLICANT_NAME on the last line.

HARD RULES — NEVER DO THESE:
- Never remove, shorten, reorder, or merge any point from CORE_IDENTITY_TEXT.
- Never add any fact, number, achievement, or claim that isn't in
  CORE_IDENTITY_TEXT or in EXTRA_SENTENCES.
- Never invent your own extra sentences, metaphors, or flourishes, even if
  you think they would sound nice — that is not your job here.
- Never change GREETING_LINE.
- If unsure whether a change is allowed: don't make it.

LENGTH: roughly 200-320 words total. CORE_IDENTITY_TEXT is already sized
for this — don't pad it out or cut it down.

Respond with ONLY this JSON object — no markdown fences, no text before or
after it:
{
  "subject": "short, specific subject line mentioning the company and role",
  "body": "the full email body as plain text, with \\n\\n between paragraphs"
}
"""


def generate_email(client, model: str, core_identity: dict, applicant_name: str,
                    company_context: dict, company_name: str,
                    extra_mentions_config: list, target_role: str, greeting: str) -> dict:
    """
    Produces {"subject": ..., "body": ...} for one company.
    `greeting` is the pre-resolved exact opening line (e.g. "Hello Charly,"
    or "Hello Rtone team,") — computed in utils.build_greeting, not left to
    the AI to guess.
    """
    matched_ids = company_context.get("matched_extra_mentions", [])

    # Each matched extra_mention contributes its pre-written `sentence`
    # as-is — the model is asked to insert it near-verbatim, not to write
    # its own sentence from a topic instruction. Less for a small/free
    # model to improvise, less that can drift from the approved wording.
    extra_sentences = [
        m["sentence"] for m in extra_mentions_config if m["id"] in matched_ids
    ][:2]  # hard cap of 2, matching the system prompt's rule

    payload = {
        "CORE_IDENTITY_TEXT": core_identity["angle_prompt"].replace(
            "{company_name}", company_name
        ),
        "APPLICANT_NAME": applicant_name,
        "GREETING_LINE": greeting,
        "COMPANY_NAME": company_name,
        "TARGET_ROLE": target_role,
        "EXTRA_SENTENCES": extra_sentences,
    }

    # hy3 is a reasoning model that often returns empty drafts — prefer these for writing.
    # Transport-level errors (network, 429, 5xx) are already retried inside
    # CompatibleAIClient.messages.create; retry_on here only covers a
    # successful HTTP call that came back empty or unparsable, and only
    # once per model, before falling through to the next model.
    last_err = None
    for attempt_model in build_model_fallback_list(model):
        def _call_and_parse(model_name=attempt_model):
            resp = client.messages.create(
                model=model_name,
                max_tokens=4096,
                system=WRITER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(payload, indent=2)}],
            )
            raw_text = resp.content[0].text.strip()
            if not raw_text:
                raise ValueError("Writer agent returned empty content")
            cleaned = extract_json_object(raw_text) or raw_text
            data = json.loads(cleaned)
            if "subject" not in data or "body" not in data:
                raise ValueError(f"Writer agent response missing subject/body: {data}")
            subject = (data.get("subject") or "").strip()
            body = (data.get("body") or "").strip()
            if not subject or not body:
                raise ValueError(f"Writer agent returned empty subject/body: {data}")
            return {"subject": subject, "body": body}

        try:
            result = with_retry(
                _call_and_parse,
                attempts=2, base_delay=1.0,
                retry_on=(ValueError, json.JSONDecodeError),
                what=f"Writer call for {company_name} ({attempt_model})",
            )
            if attempt_model != model:
                print(f"    [writer] used fallback model {attempt_model} for {company_name}")
            return result
        except Exception as e:
            last_err = e
            print(f"    [writer] model {attempt_model} failed for {company_name}: {e}")
            continue

    raise last_err or RuntimeError(f"All writer models failed for {company_name}")
