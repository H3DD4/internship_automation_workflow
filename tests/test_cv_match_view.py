"""Companies whose website matched nothing on the CV.

Their emails leave out the "why I fit" paragraph, so they're kept out of the
everyday tabs and counts — but never deleted or blocked, because matching
isn't perfect: they have their own tab, and search and favorites find them.
"""

import pytest


@pytest.fixture
def two_drafts(make_app):
    match = make_app(company="Matchco", email="jobs@matchco.com", status="ready",
                     hook_status="verified", matched_extra_mentions='["cybersecurity"]')
    nomatch = make_app(company="Nomatchco", email="jobs@nomatchco.com", status="ready",
                       hook_status="verified", matched_extra_mentions="[]")
    return match, nomatch


def _overview(client, **params):
    return client.get("/api/overview", query_string=params).get_json()


def test_the_everyday_tabs_hide_companies_with_no_cv_match(client, two_drafts):
    for status in ("", "ready"):
        data = _overview(client, status=status)
        assert "Matchco" in data["rows_html"] and "Nomatchco" not in data["rows_html"]
    data = _overview(client)
    assert data["grouped"]["ready"] == 1 and data["no_match_count"] == 1


def test_they_have_their_own_tab(client, two_drafts):
    data = _overview(client, status="no_match")
    assert "Nomatchco" in data["rows_html"] and "Matchco" not in data["rows_html"].replace("Nomatchco", "")
    page = client.get("/?status=no_match").data.decode()
    assert "matched nothing on your CV" in page and "No match" in page


def test_the_main_page_says_how_many_are_hidden(client, two_drafts):
    page = client.get("/").data.decode()
    assert "with no match to your CV" in page and "status=no_match" in page


def test_search_and_favorites_still_find_them(client, two_drafts, data):
    assert "Nomatchco" in _overview(client, q="nomatch")["rows_html"]
    data.set_favorite([two_drafts[1]], True)
    assert "Nomatchco" in _overview(client, status="favorites")["rows_html"]


def test_unresearched_and_sent_companies_are_never_hidden(client, make_app):
    make_app(company="Fresh", email="a@fresh.com", status="pending", subject="", body="")
    make_app(company="Oldsend", email="a@oldsend.com", status="sent",
             hook_status="verified", matched_extra_mentions="[]")
    rows = _overview(client)["rows_html"]
    assert "Fresh" in rows and "Oldsend" in rows


def test_a_no_match_draft_can_still_be_sent(client, two_drafts, data):
    """Hidden from view, not blocked: the user decides."""
    from db import SENDABLE_STATUSES
    row = data.get_application_by_id(two_drafts[1])
    assert row["status"] in SENDABLE_STATUSES and row["subject"]
