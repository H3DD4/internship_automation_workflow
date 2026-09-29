"""One place that turns (user, company, research) into an email draft.

The pipeline's writer, the dashboard's "Rebuild draft" and the EN/FR switch
all call compose_for(), so the three can never disagree about what a
company's email says.
"""

from __future__ import annotations

from dataclasses import dataclass

import language
import profiles
from agents.composer import compose_email
from user_config import UserConfig
from utils import build_greeting


class NotReady(RuntimeError):
    """The user hasn't finished their profile yet."""


@dataclass
class DraftingConfig:
    user_id: int
    applicant_name: str
    specs: dict            # {"en": spec, "fr": spec or None}
    target_roles: dict     # {"en": ..., "fr": ...}
    language_mode: str     # auto | en | fr

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
                          language_mode=profile.get("language_mode") or "auto")


def pick_language(dcfg: DraftingConfig, app: dict, research: dict | None,
                  override: str | None = None) -> str:
    return language.choose_language(
        override=override, default_mode=dcfg.language_mode, context=research,
        email=app.get("email", ""), website=app.get("website", ""),
        available=dcfg.available_languages)


def compose_for(dcfg: DraftingConfig, app: dict, research: dict | None,
                lang: str | None = None) -> dict:
    """{"subject", "body", "language"} for one application. `lang` forces a
    language (the EN/FR switch); otherwise the usual rule decides."""
    lang = pick_language(dcfg, app, research, override=lang)
    spec = dcfg.specs[lang]
    greeting = build_greeting(app.get("contact_name"), app["company_name"], lang)
    draft = compose_email(spec, language.research_for_language(research, lang),
                          app["company_name"], greeting, dcfg.applicant_name,
                          dcfg.target_roles[lang], lang)
    return {**draft, "language": lang}
