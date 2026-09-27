"""Preparation pipeline: resume rules, stop handling, recovery, loading."""

from unittest.mock import patch

import cache_store
import db
from pipeline import Pipeline, needs_preparation, _load_research


class FakeClient:
    """Stands in for CompatibleAIClient — no test may reach the network."""


def _pipeline():
    cfg = {
        "extra_mentions": [],
        "core_identity": {"angle_prompt": "pitch"},
        "applicant_name": "Me",
        "target_role": "Intern",
    }
    return Pipeline(FakeClient(), cfg, "model", research_workers=1, writer_workers=1)


def test_needs_preparation_skips_finished_rows(isolated, make_app):
    ready = db.get_application_by_id(make_app(status="ready"))
    assert needs_preparation(ready) is False

    sent = db.get_application_by_id(make_app(company="S", email="s@x.com", status="sent"))
    assert needs_preparation(sent) is False

    skipped = db.get_application_by_id(make_app(company="K", email="k@x.com", status="skipped"))
    assert needs_preparation(skipped) is False

    fresh_id = db.get_or_create_application("New", "new@x.com", "")
    assert needs_preparation(db.get_application_by_id(fresh_id)) is True


def test_writer_task_reuses_a_cached_draft(isolated):
    email = "cached@x.com"
    cache_store.save_draft(email, {"subject": "Cached", "body": "Cached body"})
    cache_store.save_research(email, {"industry": "tech", "matched_extra_mentions": []})
    app_id = db.get_or_create_application("PipeCo", email, "https://pipe.com")

    pipeline = _pipeline()
    with patch("builtins.print"):
        pipeline._writer_task(app_id, "PipeCo", email, "https://pipe.com", "",
                               cache_store.load_research(email))

    row = db.get_application_by_id(app_id)
    assert row["status"] == "ready"
    assert row["subject"] == "Cached"
    assert pipeline.results["ready"] == 1


def test_stop_flag_prevents_new_work(isolated):
    """Regression: the stop file was only checked while queueing futures,
    which finishes instantly — so "Stop safely" never stopped anything."""
    app_id = db.get_or_create_application("StopCo", "stop@x.com", "https://stop.com")
    pipeline = _pipeline()
    pipeline._shutdown.set()

    with patch("builtins.print"):
        pipeline._research_task(("StopCo", "stop@x.com", "https://stop.com", ""))
        pipeline._writer_task(app_id, "StopCo", "stop@x.com", "https://stop.com", "", {})

    assert pipeline.results["skipped"] == 2
    assert pipeline.results["ready"] == 0


def test_stopped_run_accounts_for_cancelled_work(isolated):
    """Work cancelled by the stop request never runs, so it never records an
    outcome — without reconciling it, the run summary warns that companies
    were lost when the user simply pressed Stop."""
    rows = [(f"C{i}", f"c{i}@x.com", "", "") for i in range(3)]
    pipeline = _pipeline()
    pipeline._stop_file = str(isolated / "stop.flag")
    (isolated / "stop.flag").touch()

    with patch("builtins.print"):
        results = pipeline.run(rows)

    assert sum(results.values()) == len(rows)
    assert results["ready"] == 0


def test_research_context_rebuilt_from_db_when_cache_missing(isolated):
    app_id = db.get_or_create_application("R", "r@x.com", "")
    db.update_application(app_id, status="researched", industry="Tech",
                          talking_points='["cloud"]',
                          matched_extra_mentions='["cloud"]',
                          match_reasons='{"cloud": "they use aws"}')
    context = _load_research("r@x.com", db.get_application_by_id(app_id))
    assert context["industry"] == "Tech"


def test_stale_in_progress_rows_are_recovered(isolated, make_app):
    """A killed run leaves rows mid-stage; nothing revisits them, so the
    dashboard showed them as permanently "in progress"."""
    researching = make_app(company="A", email="a@x.com", status="researching",
                           subject="", body="")
    writing = make_app(company="B", email="b@x.com", status="writing", subject="", body="")

    recovered = db.recover_stale_preparation_rows()

    assert recovered == 2
    assert db.get_application_by_id(researching)["status"] == "pending"
    assert db.get_application_by_id(writing)["status"] == "researched"


def test_init_db_keeps_error_message_on_repeat_calls(isolated):
    """Regression: init_db ran on every dashboard request and its migration
    cleared error_message, so a real writer failure vanished on refresh."""
    app_id = db.get_or_create_application("E", "e@x.com", "")
    db.update_application(app_id, status="failed", industry="tech",
                          error_message="Writer agent error: boom")

    db.init_db()
    db.init_db()

    row = db.get_application_by_id(app_id)
    assert row["error_message"] == "Writer agent error: boom"
