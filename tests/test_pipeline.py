"""Preparation pipeline: resume rules, stop handling, recovery, languages."""

from unittest.mock import patch

import cache_store
import db
import drafting
import pipeline as pipeline_module
from pipeline import Pipeline, _load_research, needs_preparation


class FakeClient:
    """Stands in for the AI router — no test may reach the network."""


def _pipeline(user_id, stop_check=None):
    dcfg = drafting.load_config(user_id)
    return Pipeline(user_id, FakeClient(), dcfg, "model", research_workers=1, writer_workers=1,
                    stop_check=stop_check)


def test_needs_preparation_skips_finished_rows(with_profile, make_app, data, user_id):
    ready = data.get_application_by_id(make_app(status="ready"))
    assert needs_preparation(user_id, ready) is False
    sent = data.get_application_by_id(make_app(company="S", email="s@x.com", status="sent"))
    assert needs_preparation(user_id, sent) is False
    skipped = data.get_application_by_id(make_app(company="K", email="k@x.com", status="skipped"))
    assert needs_preparation(user_id, skipped) is False
    fresh_id = data.get_or_create_application("New", "new@x.com", "")
    assert needs_preparation(user_id, data.get_application_by_id(fresh_id)) is True


def test_writer_task_reuses_a_cached_draft(with_profile, data, user_id):
    email = "cached@x.com"
    cache_store.save_draft(user_id, email, {"subject": "Cached", "body": "Cached body"})
    cache_store.save_research(user_id, email, {"industry": "tech", "matched_extra_mentions": []})
    app_id = data.get_or_create_application("PipeCo", email, "https://pipe.com")
    pipeline = _pipeline(user_id)
    with patch("builtins.print"):
        pipeline._writer_task(app_id, "PipeCo", email, "https://pipe.com", "",
                              cache_store.load_research(user_id, email))
    row = data.get_application_by_id(app_id)
    assert row["status"] == "ready"
    assert row["subject"] == "Cached"
    assert pipeline.results["ready"] == 1


def test_writer_drafts_french_for_a_french_site(with_profile, data, user_id):
    app_id = data.get_or_create_application("Société Test", "rh@test.fr", "https://test.fr")
    research = {"company_hook": "", "areas": ["offensive_security"], "hook_status": "none offered",
                "site_language": "fr"}
    pipeline = _pipeline(user_id)
    with patch("builtins.print"):
        pipeline._writer_task(app_id, "Société Test", "rh@test.fr", "https://test.fr", "", research)
    row = data.get_application_by_id(app_id)
    assert row["status"] == "ready"
    assert row["language"] == "fr"
    assert row["body"].startswith("Madame, Monsieur,")
    assert "sécurité offensive" in row["body"]


def test_writer_drafts_english_for_an_english_site(with_profile, data, user_id):
    app_id = data.get_or_create_application("Acme", "jobs@acme.com", "https://acme.com")
    research = {"company_hook": "", "areas": [], "hook_status": "none offered", "site_language": "en"}
    pipeline = _pipeline(user_id)
    with patch("builtins.print"):
        pipeline._writer_task(app_id, "Acme", "jobs@acme.com", "https://acme.com", "", research)
    row = data.get_application_by_id(app_id)
    assert row["language"] == "en"
    assert row["body"].startswith("Dear Acme Team,")


def test_stop_flag_prevents_new_work(with_profile, data, user_id):
    """Regression: "Stop safely" must stop work that hasn't started yet."""
    app_id = data.get_or_create_application("StopCo", "stop@x.com", "https://stop.com")
    pipeline = _pipeline(user_id)
    pipeline._shutdown.set()
    with patch("builtins.print"):
        pipeline._research_task(("StopCo", "stop@x.com", "https://stop.com", ""))
        pipeline._writer_task(app_id, "StopCo", "stop@x.com", "https://stop.com", "", {})
    assert pipeline.results["skipped"] == 2
    assert pipeline.results["ready"] == 0


def test_stopped_run_accounts_for_cancelled_work(with_profile, user_id):
    """Cancelled work is counted as skipped, not reported as "lost"."""
    rows = [(f"C{i}", f"c{i}@x.com", "", "") for i in range(3)]
    pipeline = _pipeline(user_id, stop_check=lambda: True)
    with patch("builtins.print"):
        results = pipeline.run(rows)
    assert sum(results.values()) == len(rows)
    assert results["ready"] == 0


def test_research_context_rebuilt_from_db_when_cache_missing(data, user_id):
    app_id = data.get_or_create_application("R", "r@x.com", "")
    data.update_application(app_id, status="researched", industry="Tech",
                            matched_extra_mentions='["cloud"]',
                            match_reasons='{"cloud": "3 keyword(s) on the site"}',
                            company_hook="managed cloud platforms", hook_status="grounded")
    context = _load_research(user_id, "r@x.com", data.get_application_by_id(app_id))
    assert context["industry"] == "Tech"
    assert context["areas"] == ["cloud"]
    assert context["company_hook"] == "managed cloud platforms"


def test_research_from_before_the_current_format_is_redone(data, user_id):
    email = "old@x.com"
    cache_store.save_research(user_id, email, {"industry": "tech", "matched_extra_mentions": ["cloud"]})
    app_id = data.get_or_create_application("Old", email, "")
    data.update_application(app_id, status="researched", industry="tech")
    assert _load_research(user_id, email, data.get_application_by_id(app_id)) is None


def test_the_cache_is_per_user(make_user, user_id):
    other = make_user("other@example.com")
    cache_store.save_draft(user_id, "same@x.com", {"subject": "Mine", "body": "b"})
    assert cache_store.load_draft(other, "same@x.com") is None
    assert cache_store.load_draft(user_id, "same@x.com")["subject"] == "Mine"


def test_stale_in_progress_rows_are_recovered(make_app, data):
    researching = make_app(company="A", email="a@x.com", status="researching", subject="", body="")
    writing = make_app(company="B", email="b@x.com", status="writing", subject="", body="")
    assert data.recover_stale_preparation_rows() == 2
    assert data.get_application_by_id(researching)["status"] == "pending"
    assert data.get_application_by_id(writing)["status"] == "researched"


def test_recovered_rows_drop_an_old_error(make_app, data):
    # A row interrupted mid-draft must not keep showing a failure from an
    # earlier attempt (e.g. one carried over from the single-user install).
    app_id = make_app(company="C", email="c@x.com", status="writing", subject="", body="",
                      error_message="Research error: [WinError 32] file in use")
    data.recover_stale_preparation_rows()
    row = data.get_application_by_id(app_id)
    assert row["status"] == "researched" and not row["error_message"]


def test_init_db_keeps_error_message_on_repeat_calls(data):
    app_id = data.get_or_create_application("E", "e@x.com", "")
    data.update_application(app_id, status="failed", industry="tech",
                            error_message="Writer agent error: boom")
    db.init_db()
    db.init_db()
    assert data.get_application_by_id(app_id)["error_message"] == "Writer agent error: boom"


def test_select_rows_skips_finished_companies_in_list_order(with_profile, make_app, data, user_id):
    data.add_companies([{"email": f"c{i}@x.com", "company_name": f"C{i}", "website": "", "contact_name": ""}
                        for i in range(5)])
    make_app(company="C1", email="c1@x.com", status="sent")
    make_app(company="C3", email="c3@x.com", status="ready")
    rows, skipped = pipeline_module.select_rows(user_id)
    assert [r[1] for r in rows] == ["c0@x.com", "c2@x.com", "c4@x.com"]
    assert skipped == 2
    limited, _ = pipeline_module.select_rows(user_id, limit=2)
    assert len(limited) == 2
