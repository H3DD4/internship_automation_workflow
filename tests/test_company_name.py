"""The company name written in an email.

A list gathered by scanning sometimes holds a product instead of the company
— "Opigno LMS" for Connect-i — and the email then said "What interests me
most about Opigno LMS is your work on penetration testing", praising the
product for the company's other work. The website's own name fixes it, but
only when the evidence is clear, so a list's correct name is never replaced.
"""

import pytest

from utils import name_for_email


@pytest.mark.parametrize("list_name, site_name, website, email, expected", [
    # A product in the list, the company on the site and in the domain.
    ("Opigno LMS", "Connect-i", "https://connect-i.ch", "aminck@connect-i.ch", "Connect-i"),
    ("Twintag", "Esoptra", "https://esoptra.com", "", "Esoptra"),
    ("Qonvo", "Qonto", "", "jobs@qonto.com", "Qonto"),            # a typo from the scan
    # The list already matches the domain: keep the user's own spelling.
    ("STS", "STS AG", "https://sts.ch", "", "STS"),
    ("Euro Tech Conseil", "ETC Info", "https://etcinfo.fr", "", "Euro Tech Conseil"),
    ("IMT - Intelligence in Medical Technologies", "IIMT", "https://iimt.fr", "",
     "IMT - Intelligence in Medical Technologies"),
    ("Deep Neuron Lab", "DNLab", "https://dnlab.de", "", "Deep Neuron Lab"),
    # Not enough evidence: the list wins.
    ("Acme", "Globex", "https://acme.com", "", "Acme"),          # site name doesn't match the domain
    ("Foo", "Bar", "", "foo@gmail.com", "Foo"),                   # a mailbox provider says nothing
    ("Some Name", "", "https://x.com", "", "Some Name"),          # the site didn't say
])
def test_the_name_in_the_email(list_name, site_name, website, email, expected):
    assert name_for_email(list_name, site_name, website, email) == expected


def test_the_site_name_must_appear_in_the_page_text():
    from agents.research_agent import verify_site_name
    page = "Connect‑i develops and delivers Opigno Enterprise, an award-winning platform."
    assert verify_site_name("Connect-i", page) == "Connect-i"      # any hyphen matches
    assert verify_site_name("Connect‑i", page) == "Connect-i"
    assert verify_site_name("Connect Innovations SA", page) == ""  # not on the page: invented
    assert verify_site_name("", page) == ""
    assert verify_site_name("Hortis SA", "Hortis rassemble une équipe de consultants") == "Hortis"


def test_research_returns_the_sites_own_name(monkeypatch):
    from agents import research_agent
    page = ("Connect‑i develops and delivers Opigno Enterprise. We also provide advanced "
            "cybersecurity services, including penetration testing, for multinational corporations. ") * 4
    monkeypatch.setattr(research_agent, "fetch_website_text", lambda url: page)
    monkeypatch.setattr(research_agent, "ask_json", lambda *a, **k: (
        {"areas": [], "hook": "", "hook_evidence": "", "industry": "", "summary": "",
         "organisation": "Connect-i"}, "test-model"))
    context = research_agent.get_company_context(None, "m", "Opigno LMS", "https://connect-i.ch", [])
    assert context["site_company_name"] == "Connect-i"


def test_the_draft_uses_the_companys_name_not_the_product(with_profile, make_app, data):
    import drafting
    from user_config import UserConfig
    app_id = make_app(company="Opigno LMS", email="careers@connect-i.ch", status="researched",
                      subject="", body="", website="https://connect-i.ch")
    app = data.get_application_by_id(app_id)
    dcfg = drafting.load_config(with_profile, UserConfig(with_profile))
    research = {"company_hook": "", "areas": [], "site_company_name": "Connect-i",
                "site_language": "en", "hook_status": "none offered"}
    body = drafting.compose_for(dcfg, app, research, lang="en")["body"]
    assert "Connect-i" in body and "Opigno" not in body


def test_the_company_page_says_which_name_was_used(client, make_app, user_id):
    import cache_store
    app_id = make_app(company="Opigno LMS", email="careers@connect-i.ch", website="https://connect-i.ch")
    cache_store.save_research(user_id, "careers@connect-i.ch",
                              {"site_company_name": "Connect-i", "areas": [], "company_hook": ""})
    page = client.get(f"/company/{app_id}").data.decode()
    assert "Name in email" in page and "Connect-i" in page
