"""One place that turns (user, company, research) into an email draft.

The pipeline's writer, the dashboard's "Rebuild draft" and the EN/FR switch
all call compose_for(), so the three can never disagree about what a
company's email says.
"""

from __future__ import annotations

from dataclasses import dataclass

import email_templates
import language
import profiles
from agents.composer import compose_email
from user_config import UserConfig
from utils import build_greeting, name_for_email


class NotReady(RuntimeError):
    """The user hasn't finished their profile yet."""


@dataclass
class DraftingConfig:
    user_id: int
    applicant_name: str
    specs: dict            # {"en": spec, "fr": spec or None}
    target_roles: dict     # {"en": ..., "fr": ...}
    language_mode: str     # auto | en | fr
    facts: dict = None     # the profile's facts, so any style can be built on the spot
    template_id: str = ""  # the profile's own style ("custom" = hand-written wording)

    @property
    def available_languages(self) -> tuple:
        return tuple(lang for lang in language.LANGUAGES if self.specs.get(lang))

    @property
    def research_areas(self) -> list:
        """Areas the research agent matches against. Ids are shared by both
        languages; the English spec carries the English + French keywords."""
        return (self.specs.get("en") or self.specs.get("fr"))["areas"]


def load_config(user_id: int, cfg: UserConfig | None = None) -> DraftingConfig:
    cfg = cfg or UserConfig(user_id)
    profile, spec_en, spec_fr = profiles.specs_for(user_id)
    if not spec_en and not spec_fr:
        raise NotReady("Set up your profile first (Profile page).")
    facts = profile.get("facts") or {}
    name = (facts.get("full_name") or "").strip() or cfg.get("YOUR_NAME")
    if not name:
        raise NotReady("Add your name to your profile first.")
    fallback_role = cfg.get("YOUR_TARGET_ROLE")
    roles = {}
    for lang, spec in (("en", spec_en), ("fr", spec_fr)):
        role = ((spec or {}).get("target_role") or "").strip()
        roles[lang] = role or fallback_role or ("Internship" if lang == "en" else "Stage")
    return DraftingConfig(user_id=user_id, applicant_name=name,
                          specs={"en": spec_en, "fr": spec_fr}, target_roles=roles,
                          language_mode=profile.get("language_mode") or "auto",
                          facts=facts, template_id=profile.get("template_id") or "")


def pick_language(dcfg: DraftingConfig, app: dict, research: dict | None,
                  override: str | None = None) -> str:
    return language.choose_language(
        override=override, default_mode=dcfg.language_mode, context=research,
        email=app.get("email", ""), website=app.get("website", ""),
        available=dcfg.available_languages)


def style_choices(dcfg: DraftingConfig, lang: str = "en") -> list:
    """The styles one email can be switched to: the profile's own
    hand-written wording (when it has one) and every template its facts can
    fill."""
    choices = []
    if dcfg.template_id == "custom":
        choices.append({"id": "custom", "name": "Your own wording" if lang == "en" else "Votre propre texte",
                        "description": "The hand-tuned wording on your profile.", "best_for": "", "evidence": []})
    if dcfg.facts:
        choices += email_templates.template_choices(lang)
    elif dcfg.template_id in email_templates.TEMPLATES:
        choices += [c for c in email_templates.template_choices(lang) if c["id"] == dcfg.template_id]
    return choices


def spec_for_style(dcfg: DraftingConfig, style: str | None, lang: str) -> dict:
    """The wording for `style` in `lang`: the profile's own specs for its own
    style, any other template built from the profile's facts on the spot."""
    if not style or style == dcfg.template_id or style not in email_templates.TEMPLATES or not dcfg.facts:
        return dcfg.specs[lang]
    spec = email_templates.build_spec(dcfg.facts, style, lang)
    own = dcfg.specs.get(lang) if dcfg.template_id == "custom" else None
    if own:
        # A user with hand-written wording keeps their own content — the
        # areas research matches companies against (same ids, so the "your
        # focus on X is where my experience is strongest" paragraph appears)
        # and their own strengths — and the style changes only the wording
        # around it.
        spec["areas"] = own.get("areas") or spec["areas"]
        spec["strengths"] = own.get("strengths") or spec["strengths"]
        own_email = own.get("email") or {}
        for key in ("motivation", "default_topic"):
            if own_email.get(key):
                spec["email"][key] = own_email[key]
        allowed = set(spec["verified_facts"].get("allowed_numbers") or [])
        allowed |= set((own.get("verified_facts") or {}).get("allowed_numbers") or [])
        spec["verified_facts"] = {**spec["verified_facts"], "allowed_numbers": sorted(allowed)}
    problems = email_templates.spec_problems(spec)
    if problems:
        raise NotReady(f"Your profile has no {lang.upper()} wording for {', '.join(problems)} yet.")
    return spec


def compose_for(dcfg: DraftingConfig, app: dict, research: dict | None,
                lang: str | None = None, style: str | None = None) -> dict:
    """{"subject", "body", "language", "template_id"} for one application.
    `lang` forces a language (the EN/FR switch), `style` an email style (the
    style switch); otherwise this company's saved style, then the profile's."""
    lang = pick_language(dcfg, app, research, override=lang)
    style = style or app.get("template_id") or None
    spec = spec_for_style(dcfg, style, lang)
    company = email_company_name(app, research)
    greeting = build_greeting(app.get("contact_name"), company, lang)
    draft = compose_email(spec, language.research_for_language(research, lang),
                          company, greeting, dcfg.applicant_name,
                          dcfg.target_roles[lang], lang)
    chosen = style if style and style != dcfg.template_id and spec is not dcfg.specs[lang] else None
    return {**draft, "language": lang, "template_id": chosen}


def email_company_name(app: dict, research: dict | None) -> str:
    """The name the email uses: the list's, unless the website shows the list
    holds a product name instead of the company (see utils.name_for_email)."""
    return name_for_email(app.get("company_name") or "", (research or {}).get("site_company_name"),
                          app.get("website") or "", app.get("email") or "")
